from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user

from models import db, User

auth_bp = Blueprint("auth_bp", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Render login form and authenticate the user on POST."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("index"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """Render registration form and create a new user on POST."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if User.query.filter_by(username=username).first():
            flash("Username already taken.", "danger")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return render_template("register.html")

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("auth_bp.login"))

    return render_template("register.html")


@auth_bp.route("/logout")
def logout():
    """Log out the current user and redirect to the home page."""
    logout_user()
    return redirect(url_for("index"))
