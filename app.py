import os
from datetime import datetime
from functools import wraps

from flask import Flask, request, session, redirect, url_for, flash, render_template, g, abort
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader
from sqlalchemy import UniqueConstraint
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")
db_url = os.environ.get("DATABASE_URL", "sqlite:///stockflow.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


# ---------------- Models ----------------
class Organization(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    default_threshold = db.Column(db.Integer, default=5, nullable=False)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(300), nullable=False)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False)


class Product(db.Model):
    __table_args__ = (UniqueConstraint("org_id", "sku", name="uq_org_sku"),)
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    sku = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    quantity = db.Column(db.Integer, default=0, nullable=False)
    cost_price = db.Column(db.Float)
    selling_price = db.Column(db.Float)
    low_stock_threshold = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_updated_by = db.Column(db.String(200))


# ---------------- Helpers ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        uid = session.get("uid")
        user = db.session.get(User, uid) if uid else None
        if not user:
            session.clear()
            return redirect(url_for("login"))
        g.user = user
        g.org = db.session.get(Organization, user.org_id)
        return f(*a, **kw)
    return wrapper


def threshold_for(p):
    return p.low_stock_threshold if p.low_stock_threshold is not None else g.org.default_threshold


def is_low(p):
    return p.quantity <= threshold_for(p)


app.jinja_env.globals.update(is_low=is_low, threshold_for=threshold_for)


def to_int(v, default=None):
    v = (v or "").strip()
    if v == "":
        return default
    return int(v)


def to_float(v):
    v = (v or "").strip()
    return float(v) if v else None


def get_product(pid):
    p = Product.query.filter_by(id=pid, org_id=g.org.id).first()  # tenant scoping
    if not p:
        abort(404)
    return p


def fill_product(p, form):
    """Validate form and fill product. Returns error string or None."""
    name, sku = form.get("name", "").strip(), form.get("sku", "").strip()
    if not name or not sku:
        return "Name and SKU are required."
    try:
        qty = to_int(form.get("quantity"), 0)
        cost, sell = to_float(form.get("cost_price")), to_float(form.get("selling_price"))
        thr = to_int(form.get("low_stock_threshold"))
    except ValueError:
        return "Quantity, prices and threshold must be valid numbers."
    if qty < 0 or (cost is not None and cost < 0) or (sell is not None and sell < 0) or (thr is not None and thr < 0):
        return "Values cannot be negative."
    dup = Product.query.filter(Product.org_id == g.org.id, Product.sku == sku, Product.id != (p.id or 0)).first()
    if dup:
        return "SKU already exists in your organization."
    p.name, p.sku, p.description = name, sku, form.get("description", "").strip()
    p.quantity, p.cost_price, p.selling_price, p.low_stock_threshold = qty, cost, sell, thr
    p.last_updated_by = g.user.email
    return None


# ---------------- Auth ----------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
        org = request.form.get("org", "").strip()
        if "@" not in email or "." not in email:
            flash("Enter a valid email.")
        elif len(pw) < 6:
            flash("Password must be at least 6 characters.")
        elif pw != pw2:
            flash("Passwords do not match.")
        elif not org:
            flash("Organization name is required.")
        elif User.query.filter_by(email=email).first():
            flash("Email already registered.")
        else:
            o = Organization(name=org)
            db.session.add(o)
            db.session.flush()
            u = User(email=email, password_hash=generate_password_hash(pw), org_id=o.id)
            db.session.add(u)
            db.session.commit()
            session["uid"] = u.id
            return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
        if u and check_password_hash(u.password_hash, request.form.get("password", "")):
            session["uid"] = u.id
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- Dashboard ----------------
@app.route("/")
@login_required
def dashboard():
    products = Product.query.filter_by(org_id=g.org.id).all()
    low = [p for p in products if is_low(p)]
    return render_template("dashboard.html", total=len(products),
                           units=sum(p.quantity for p in products), low=low)


# ---------------- Products ----------------
@app.route("/products")
@login_required
def products():
    q = request.args.get("q", "").strip()
    query = Product.query.filter_by(org_id=g.org.id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.sku.ilike(like)))
    return render_template("products.html", items=query.order_by(Product.name).all(), q=q)


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def product_new():
    p = Product(org_id=g.org.id)
    if request.method == "POST":
        err = fill_product(p, request.form)
        if err:
            flash(err)
        else:
            db.session.add(p)
            db.session.commit()
            flash("Product created.")
            return redirect(url_for("products"))
    return render_template("form.html", p=p, form=request.form, title="Add Product")


@app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@login_required
def product_edit(pid):
    p = get_product(pid)
    if request.method == "POST":
        err = fill_product(p, request.form)
        if err:
            db.session.rollback()
            flash(err)
        else:
            db.session.commit()
            flash("Product updated.")
            return redirect(url_for("products"))
    return render_template("form.html", p=p, form=request.form, title="Edit Product")


@app.route("/products/<int:pid>/delete", methods=["POST"])
@login_required
def product_delete(pid):
    db.session.delete(get_product(pid))
    db.session.commit()
    flash("Product deleted.")
    return redirect(url_for("products"))


@app.route("/products/<int:pid>/adjust", methods=["POST"])
@login_required
def product_adjust(pid):
    p = get_product(pid)
    try:
        delta = int(request.form.get("delta", ""))
    except ValueError:
        flash("Enter a whole number like +5 or -3.")
        return redirect(url_for("products"))
    if p.quantity + delta < 0:
        flash("Stock cannot go below zero.")
    else:
        p.quantity += delta
        p.last_updated_by = g.user.email
        db.session.commit()
        flash(f"Stock adjusted by {delta:+d}.")
    return redirect(url_for("products"))


# ---------------- Settings ----------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        try:
            v = int(request.form.get("default_threshold", ""))
            if v < 0:
                raise ValueError
            g.org.default_threshold = v
            db.session.commit()
            flash("Settings saved.")
        except ValueError:
            flash("Enter a valid non-negative whole number.")
    return render_template("settings.html")


# ---------------- Templates (inline to keep it one file) ----------------
BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockFlow</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}
nav{background:#1f2937;padding:12px 24px;display:flex;gap:18px;align-items:center}
nav a{color:#fff;text-decoration:none}nav .sp{flex:1}nav b{color:#fff}
main{max-width:960px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:8px;padding:18px;box-shadow:0 1px 3px #0002;margin-bottom:16px}
.cards{display:flex;gap:16px}.cards .card{flex:1}.big{font-size:2rem;font-weight:700}
table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:8px;border-bottom:1px solid #eee;text-align:left}
input,textarea{padding:8px;border:1px solid #ccc;border-radius:4px;width:100%;box-sizing:border-box;margin:4px 0 12px}
button,.btn{background:#2563eb;color:#fff;border:0;padding:8px 14px;border-radius:4px;cursor:pointer;text-decoration:none;display:inline-block}
.danger{background:#dc2626}.badge{background:#fee2e2;color:#b91c1c;padding:2px 8px;border-radius:10px;font-size:.8rem}
.ok{background:#dcfce7;color:#166534;padding:2px 8px;border-radius:10px;font-size:.8rem}
.flash{background:#fef3c7;padding:10px;border-radius:4px;margin-bottom:12px}
form.inline{display:inline-flex;gap:4px}form.inline input{width:70px;margin:0}
</style></head><body>
<nav><b>StockFlow</b>{% if g.user %}<a href="/">Dashboard</a><a href="/products">Products</a><a href="/settings">Settings</a>
<span class="sp"></span><span style="color:#ccc">{{ g.org.name }}</span><a href="/logout">Logout</a>{% endif %}</nav>
<main>{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}{% block body %}{% endblock %}</main></body></html>"""

AUTH = """{% extends "base.html" %}{% block body %}<div class="card" style="max-width:380px;margin:auto">
<h2>{{ heading }}</h2><form method="post">
<label>Email</label><input type="email" name="email" required>
{% if signup %}<label>Organization name</label><input name="org" required>{% endif %}
<label>Password</label><input type="password" name="password" required>
{% if signup %}<label>Confirm password</label><input type="password" name="confirm" required>{% endif %}
<button>{{ heading }}</button></form>
<p>{% if signup %}Have an account? <a href="/login">Login</a>{% else %}New? <a href="/signup">Sign up</a>{% endif %}</p></div>{% endblock %}"""

TEMPLATES = {
    "base.html": BASE,
    "login.html": '{% set heading="Login" %}{% set signup=False %}' + AUTH,
    "signup.html": '{% set heading="Sign up" %}{% set signup=True %}' + AUTH,
    "dashboard.html": """{% extends "base.html" %}{% block body %}<h2>Dashboard</h2>
<div class="cards"><div class="card">Total products<div class="big">{{ total }}</div></div>
<div class="card">Total units in stock<div class="big">{{ units }}</div></div></div>
<div class="card"><h3>Low stock items</h3>{% if low %}<table><tr><th>Name</th><th>SKU</th><th>Quantity</th><th>Threshold</th></tr>
{% for p in low %}<tr><td>{{ p.name }}</td><td>{{ p.sku }}</td><td>{{ p.quantity }}</td><td>{{ threshold_for(p) }}</td></tr>{% endfor %}</table>
{% else %}<p>No low stock items.</p>{% endif %}</div>{% endblock %}""",
    "products.html": """{% extends "base.html" %}{% block body %}<h2>Products</h2>
<div class="card" style="display:flex;gap:10px"><form method="get" style="flex:1;display:flex;gap:8px"><input name="q" value="{{ q }}" placeholder="Search name or SKU" style="margin:0"><button>Search</button></form>
<a class="btn" href="/products/new">Add Product</a></div>
<table><tr><th>Name</th><th>SKU</th><th>Qty</th><th>Status</th><th>Price</th><th>Actions</th></tr>
{% for p in items %}<tr><td>{{ p.name }}</td><td>{{ p.sku }}</td><td>{{ p.quantity }}</td>
<td>{% if is_low(p) %}<span class="badge">Low stock</span>{% else %}<span class="ok">IN STOCK</span>{% endif %}</td>
<td>{{ "%.2f"|format(p.selling_price) if p.selling_price is not none else "-" }}</td>
<td><form class="inline" method="post" action="/products/{{ p.id }}/adjust"><input name="delta" placeholder="+/-N" required><button>Adjust</button></form>
<a href="/products/{{ p.id }}/edit">Edit</a>
<form class="inline" method="post" action="/products/{{ p.id }}/delete" onsubmit="return confirm('Delete this product?')"><button class="danger">Delete</button></form></td></tr>
{% else %}<tr><td colspan="6">No products yet.</td></tr>{% endfor %}</table>{% endblock %}""",
    "form.html": """{% extends "base.html" %}{% block body %}<div class="card"><h2>{{ title }}</h2><form method="post">
{% set v = form if form else {} %}
<label>Name *</label><input name="name" required value="{{ v.get('name', p.name or '') }}">
<label>SKU *</label><input name="sku" required value="{{ v.get('sku', p.sku or '') }}">
<label>Description</label><textarea name="description">{{ v.get('description', p.description or '') }}</textarea>
<label>Quantity on hand</label><input type="number" min="0" name="quantity" value="{{ v.get('quantity', p.quantity if p.quantity is not none else 0) }}">
<label>Cost price</label><input type="number" step="0.01" min="0" name="cost_price" value="{{ v.get('cost_price', p.cost_price if p.cost_price is not none else '') }}">
<label>Selling price</label><input type="number" step="0.01" min="0" name="selling_price" value="{{ v.get('selling_price', p.selling_price if p.selling_price is not none else '') }}">
<label>Low stock threshold (blank = default)</label><input type="number" min="0" name="low_stock_threshold" value="{{ v.get('low_stock_threshold', p.low_stock_threshold if p.low_stock_threshold is not none else '') }}">
<button>Save</button> <a href="/products">Cancel</a></form></div>{% endblock %}""",
    "settings.html": """{% extends "base.html" %}{% block body %}<div class="card" style="max-width:420px"><h2>Settings</h2><form method="post">
<label>Default low stock threshold</label><input type="number" min="0" name="default_threshold" value="{{ g.org.default_threshold }}">
<button>Save</button></form></div>{% endblock %}""",
}
app.jinja_loader = DictLoader(TEMPLATES)

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
