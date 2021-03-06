# -*- encoding: utf-8 -*-
from __future__ import absolute_import
import flask
from urllib.parse import urlencode, quote
from functools import wraps
from seminars.app import app, send_email
from lmfdb.logger import make_logger
from flask import (
    render_template,
    request,
    Blueprint,
    url_for,
    redirect,
    make_response,
    session,
    send_file,
)
from flask_login import (
    login_required,
    login_user,
    current_user,
    logout_user,
    LoginManager,
)
from lmfdb.utils import flash_error
from markupsafe import Markup
from icalendar import Calendar
from io import BytesIO

from psycopg2.sql import SQL
from lmfdb import db

assert db
from seminars.utils import timezones, timestamp
from seminars.tokens import generate_timed_token, read_timed_token, read_token
import datetime


login_page = Blueprint("user", __name__, template_folder="templates")
logger = make_logger(login_page)


login_manager = LoginManager()

from .pwdmanager import userdb, SeminarsUser, SeminarsAnonymousUser


@login_manager.user_loader
def load_user(uid):
    return SeminarsUser(uid)


login_manager.login_view = "user.info"

login_manager.anonymous_user = SeminarsAnonymousUser


def get_username(uid):
    """returns the name of user @uid"""
    return SeminarsUser(uid).name


# globally define user properties and username
@app.context_processor
def ctx_proc_userdata():
    userdata = {
        "user": current_user,
        "usertime": datetime.datetime.now(tz=current_user.tz),
    }
    # used to display name of locks
    userdata["get_username"] = get_username  # this is a function
    return userdata


# blueprint specific definition of the body_class variable
@login_page.context_processor
def body_class():
    return {"body_class": "login"}


@login_page.route("/login", methods=["POST"])
def login(**kwargs):
    # login and validate the user …
    # remember = True sets a cookie to remember the user
    email = request.form["email"]
    password = request.form["password"]
    if not email or not password:
        flash_error("Oops! Wrong username or password.")
        return redirect(url_for(".info"))
    # we always remember
    remember = True  # if request.form["remember"] == "on" else False
    user = SeminarsUser(email=email)
    if user and user.check_password(password):
        # this is where we set current_user = user
        login_user(user, remember=remember)
        if user.name:
            flask.flash(Markup("Hello %s, your login was successful!" % user.name))
        else:
            flask.flash(Markup("Hello, your login was successful!"))
        logger.info("login: '%s' - '%s'" % (user.get_id(), user.name))
        return redirect(url_for(".info"))
    flash_error("Oops! Wrong username or password.")
    return redirect(url_for(".info"))


def admin_required(fn):
    """
    wrap this around those entry points where you need to be an admin.
    """

    @wraps(fn)
    @login_required
    def decorated_view(*args, **kwargs):
        logger.info("admin access attempt by %s" % current_user.get_id())
        if not current_user.is_admin:
            return flask.abort(403)  # access denied
        return fn(*args, **kwargs)

    return decorated_view


def creator_required(fn):
    """
    wrap this around those entry points where you need to be a creator.
    """

    @wraps(fn)
    @login_required
    def decorated_view(*args, **kwargs):
        logger.info("creator access attempt by %s" % current_user.get_id())
        if not current_user.is_creator:
            return flask.abort(403)  # access denied
        return fn(*args, **kwargs)

    return decorated_view


def email_confirmed_required(fn):
    """
    wrap this around those entry points where you need to be a creator.
    """

    @wraps(fn)
    @login_required
    def decorated_view(*args, **kwargs):
        logger.info("email confirmed access attempt by %s" % current_user.get_id())
        if not current_user.email_confirmed:
            flash_error("Oops, you haven't yet confirmed your email")
            return redirect(url_for("user.info"))
        return fn(*args, **kwargs)

    return decorated_view


@login_page.route("/info")
def info():
    if current_user.is_authenticated:
        title = section = "Account"
    else:
        title = section = "Login"
    return render_template(
        "user-info.html",
        info=info,
        title=title,
        section=section,
        timezones=timezones,
        user=current_user,
        session=session,
    )


# ./info again, but for POST!


@login_page.route("/set_info", methods=["POST"])
@login_required
def set_info():
    for k, v in request.form.items():
        print(k, v)
        setattr(current_user, k, v)
    previous_email = current_user.email
    if current_user.save():
        flask.flash(Markup("Thank you for updating your details!"))
    if previous_email != current_user.email:
        if send_confirmation_email(current_user.email):
            flask.flash(Markup("New confirmation email has been sent!"))
    return redirect(url_for(".info"))


@login_page.route("/seminars")
@creator_required
def list_seminars():
    raise NotImplementedError


@login_page.route("/subscriptions")
@creator_required
def list_subscriptions():
    raise NotImplementedError


@login_page.route("/send_confirmation_email")
@login_required
def resend_confirmation_email():
    if send_confirmation_email(current_user.email):
        flask.flash(Markup("New confirmation email has been sent!"))
    return redirect(url_for(".info"))


# The analogous function creator_required is not defined, since we want to allow normal users to creat things that won't be displayed until they're approved as a creator.


def housekeeping(fn):
    """
    wrap this around maintenance calls, they are only accessible for
    admins and for localhost
    """

    @wraps(fn)
    def decorated_view(*args, **kwargs):
        logger.info("housekeeping access attempt by %s" % request.remote_addr)
        if request.remote_addr in ["127.0.0.1", "localhost"]:
            return fn(*args, **kwargs)
        return admin_required(fn)(*args, **kwargs)

    return decorated_view


@login_page.route("/register/", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", title="Register", email="")
    elif request.method == "POST":
        email = request.form["email"]
        pw1 = request.form["password1"]
        pw2 = request.form["password2"]
        from email_validator import validate_email, EmailNotValidError

        try:
            validate_email(email)
        except EmailNotValidError as e:
            flash_error("""Oops, email '%s' is not allowed. %s""", email, str(e))
            return make_response(render_template("register.html", title="Register", email=email))
        if pw1 != pw2:
            flash_error("Oops, passwords do not match!")
            return make_response(render_template("register.html", title="Register", email=email))

        if len(pw1) < 8:
            flash_error("Oops, password too short. Minimum 8 characters please!")
            return make_response(render_template("register.html", title="Register", email=email))

        password = pw1
        if userdb.user_exists(email=email):
            flash_error("Sorry, email '%s' is already registered!", email)
            return make_response(render_template("register.html", title="Register", email=email))

        newuser = userdb.new_user(email=email, password=password,)

        send_confirmation_email(email)
        login_user(newuser, remember=True)
        flask.flash(Markup("Hello! Congratulations, you are a new user!"))
        logger.info("new user: '%s' - '%s'" % (newuser.get_id(), newuser.email))
        return redirect(url_for(".info"))


@login_page.route("/change_password", methods=["POST"])
@login_required
def change_password():
    email = current_user.email
    pw_old = request.form["oldpwd"]
    if not current_user.authenticate(pw_old):
        flash_error("Ooops, old password is wrong!")
        return redirect(url_for(".info"))

    pw1 = request.form["password1"]
    pw2 = request.form["password2"]
    if pw1 != pw2:
        flash_error("Oops, new passwords do not match!")
        return redirect(url_for(".info"))

    if len(pw1) < 8:
        flash_error("Oops, password too short. Minimum 8 characters please!")
        return redirect(url_for(".info"))

    userdb.change_password(email, pw1)
    flask.flash(Markup("Your password has been changed."))
    return redirect(url_for(".info"))


@login_page.route("/logout")
@login_required
def logout():
    logout_user()
    flask.flash(Markup("You are logged out now. Have a nice day!"))
    return redirect(url_for(".info"))


@login_page.route("/admin")
@login_required
@admin_required
def admin():
    return "success: only admins can read this!"


# confirm email


def generate_confirmation_token(email):
    return generate_timed_token(email, salt="confirm email")


def send_confirmation_email(email):
    token = generate_confirmation_token(email)
    confirm_url = url_for(".confirm_email", token=token, _external=True, _scheme="https")
    html = render_template("confirm_email.html", confirm_url=confirm_url)
    subject = "Please confirm your email"
    try:
        send_email(email, subject, html)
        return True
    except:
        import sys
        flash_error('Unable to send email confirmation link, please contact <a href="mailto:mathseminars@math.mit.edu">mathseminars@math.mit.edu</a> directly to confirm your email')
        app.logger.error("%s unable to send email to %s due to error: %s" % (timestamp(), email, sys.exc_info()[0]))
        return False




import os
@login_page.route("/sendlostemails")
@housekeeping
def send_lost_emails():
    def send_confirmation_email_fix(email):
        token = generate_confirmation_token(email)
        confirm_url = 'https://mathseminars.org/' + url_for(".confirm_email", token=token)
        html = render_template("confirm_email.html", confirm_url=confirm_url)
        subject = "Please confirm your email"
        send_email(email, subject, html)

    def send_reset_password_fix(email):
        token = generate_password_token(email)
        reset_url = 'https://mathseminars.org/'  + url_for(".reset_password_wtoken", token=token)
        html = render_template("reset_password_email.html", reset_url=reset_url)
        subject = "Resetting password"
        send_email(email, subject, html)
    root_path = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    out = ""
    if os.path.exists(os.path.join(root_path, 'confirmation_emails.txt')):
        with open(os.path.join(root_path, 'confirmation_emails.txt')) as F:
            out += "confirmation_emails.txt\n"
            for email in F.readlines():
                email = email.rstrip('\n')
                try:
                    send_confirmation_email_fix(email)
                    out += email +  ', success\n'
                except Exception:
                    out += email +  ', fail\n'
    if os.path.exists(os.path.join(root_path, 'reset_emails.txt')):
        with open(os.path.join(root_path, 'reset_emails.txt')) as F:
            out += "reset_emails.txt\n"
            for email in F.readlines():
                email = email.rstrip('\n')
                try:
                    send_reset_password_fix(email)
                    out += email +  ', success\n'
                except Exception:
                    out += email +  ', fail\n'

    return out.replace("\n", "<br>")



@login_page.route("/confirm/<token>")
@login_required
def confirm_email(token):
    try:
        # the users have 24h to confirm their email
        email = read_timed_token(token, "confirm email", 86400)
    except Exception:
        flash_error("The confirmation link is invalid or has expired.")
    else:
        if current_user.email.lower() != email.lower():
            flash_error("The link is not valid for this account.")
        elif current_user.email_confirmed:
            flash_error("Email already confirmed.")
        else:
            current_user.email_confirmed = True
            current_user.save()
            flask.flash("You have confirmed your email. Thanks!", "success")
    return redirect(url_for(".info"))


# reset password


def generate_password_token(email):
    return generate_timed_token(email, salt="reset password")


def send_reset_password(email):
    token = generate_password_token(email)
    reset_url = url_for(".reset_password_wtoken", token=token, _external=True, _scheme="https")
    html = render_template("reset_password_email.html", reset_url=reset_url)
    subject = "Resetting password"
    send_email(email, subject, html)


@login_page.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    if request.method == "GET":
        return render_template("reset_password_ask_email.html", title="Forgot Password",)
    elif request.method == "POST":
        email = request.form["email"]
        if userdb.user_exists(email):
            send_reset_password(email)
        flask.flash(Markup("Check your mailbox for instructions on how to reset your password"))
        return redirect(url_for(".info"))


@login_page.route("/reset/<token>", methods=["GET", "POST"])
def reset_password_wtoken(token):
    try:
        # the users have one hour to use previous token
        email = read_timed_token(token, "reset password", 3600)
    except Exception:
        flash_error("The link is invalid or has expired.")
        return redirect(url_for(".info"))
    if not userdb.user_exists(email):
        flash_error("The link is invalid or has expired.")
        return redirect(url_for(".info"))
    if request.method == "GET":
        return render_template("reset_password_wtoken.html", title="Reset password", token=token)
    elif request.method == "POST":
        pw1 = request.form["password1"]
        pw2 = request.form["password2"]
        if pw1 != pw2:
            flash_error("Oops, passwords do not match!")
            return redirect(url_for(".reset_password_wtoken", token=token))

        if len(pw1) < 8:
            flash_error("Oops, password too short. Minimum 8 characters please!")
            return redirect(url_for(".reset_password_wtoken", token=token))

        userdb.change_password(email, pw1)
        flask.flash(Markup("Your password has been changed. Please login with your new password."))
        return redirect(url_for(".info"))


# endorsement


@login_page.route("/endorse", methods=["POST"])
@login_required
@creator_required
def get_endorsing_link():
    email = request.form["email"]
    from email_validator import validate_email, EmailNotValidError

    try:
        validate_email(email)
    except EmailNotValidError as e:
        flash_error("""Oops, email '%s' is not allowed. %s""", email, str(e))
        return redirect(url_for(".info"))
    link = endorser_link(current_user, email)
    rec = userdb.lookup(email, ["name", "creator", "email_confirmed"])
    if rec is None or not rec["email_confirmed"]:  # No account or email unconfirmed
        to_send = """Hello,

I am offering you permission to add content (e.g., create a seminar)
on the mathseminars.org website.

To accept this invitation:

1. Register at https://mathseminars.org/user/register/ using this email address.

2. Click on the link the system emails you, to confirm your email address.

3. Go to {link}

Best,
{name}
""".format(
            link=link, name=current_user.name
        )
        data = {
            "body": to_send,
            "subject": "An invitation to collaborate on mathseminars.org",
        }
        endorsing_link = """
<p>
 The link to endorse {email} is</br>
 <span class="noclick">{link}</span></br>
<button onClick="window.open('mailto:{email}?{msg}')">
Send email
</button>
 </p>
""".format(
            link=link, email=email, msg=urlencode(data, quote_via=quote)
        )
    else:
        target_name = rec["name"]
        if rec["creator"]:
            endorsing_link = "<p>{target_name} is already able to create content.</p>".format(
                target_name=target_name
            )
        else:
            to_send = """Dear {target_name},

I have endorsed you on mathseminars.org and any content you create will
be publicly viewable.

Best,
{name}
""".format(
                name=current_user.name, target_name=target_name
            )
            data = {
                "body": to_send,
                "subject": "Endorsement to create content on mathseminars.org",
            }
            userdb.make_creator(email, current_user._uid)
            endorsing_link = """
<p>
 {target_name} is now able to create content.</br>
<button onClick="window.open('mailto:{email}?{msg}')">
Send email
</button> to let them know.
""".format(
                target_name=target_name, email=email, msg=urlencode(data, quote_via=quote),
            )
    session["endorsing link"] = endorsing_link
    return redirect(url_for(".info"))


def generate_endorsement_token(endorser, email):
    rec = [int(endorser.id), email]
    return generate_timed_token(rec, "endorser")


def endorser_link(endorser, email):
    token = generate_endorsement_token(endorser, email)
    return url_for(".endorse_wtoken", token=token, _external=True, _scheme="https")


@login_page.route("/endorse/<token>")
@login_required
@email_confirmed_required
def endorse_wtoken(token):
    try:
        # tokens last forever
        endorser, email = read_timed_token(token, "endorser", None)
    except Exception:
        return flask.abort(404, "The link is invalid or has expired.")
        return redirect(url_for(".info"))
    if current_user.is_creator:
        flash_error("Account already has creator privileges.")
    elif current_user.email.lower() != email.lower():
        flash_error("The link is not valid for this account.")
    else:
        userdb.make_creator(current_user.email, int(endorser))
        current_user.save()
        flask.flash("You can now create seminars. Thanks!", "success")
    return redirect(url_for(".info"))


@login_page.route("/subscribe/<shortname>")
@login_required
def seminar_subscriptions_add(shortname):
    code, msg = current_user.seminar_subscriptions_add(shortname)
    current_user.save()
    return msg, code


@login_page.route("/unsubscribe/<shortname>")
@login_required
def seminar_subscriptions_remove(shortname):
    code, msg = current_user.seminar_subscriptions_remove(shortname)
    current_user.save()
    return msg, code


@login_page.route("/subscribe/<shortname>/<ctr>")
@login_required
def talk_subscriptions_add(shortname, ctr):
    code, msg = current_user.talk_subscriptions_add(shortname, int(ctr))
    current_user.save()
    return msg, code


@login_page.route("/unsubscribe/<shortname>/<ctr>")
@login_required
def talk_subscriptions_remove(shortname, ctr):
    code, msg = current_user.talk_subscriptions_remove(shortname, int(ctr))
    current_user.save()
    return msg, code


@login_page.route("/ics/<token>")
def ics_file(token):
    try:
        uid = read_token(token, "ics")
        user = SeminarsUser(uid=int(uid))
        if not user.email_confirmed:
            return flask.abort(404, "The email has not yet been confirmed!")
    except Exception:
        return flask.abort(404, "Invalid link")

    cal = Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", "mathseminars.org")
    cal.add("CALSCALE", "GREGORIAN")
    cal.add("X-WR-CALNAME", "mathseminars.org")

    for talk in user.talks:
        cal.add_component(talk.event(user))
    for seminar in user.seminars:
        for talk in seminar.talks():
            cal.add_component(talk.event(user))
    bIO = BytesIO()
    bIO.write(cal.to_ical())
    bIO.seek(0)
    return send_file(
        bIO, attachment_filename="mathseminars.ics", as_attachment=True, add_etags=False
    )


@login_page.route("/public/")
@login_required
@email_confirmed_required
def public_users():
    query = SQL(
        "SELECT users.affiliation, users.name, users.email, users.homepage FROM users JOIN (SELECT DISTINCT email FROM seminar_organizers WHERE contact) as organizers ON users.email = organizers.email WHERE users.creator = True"
    )
    public_organizers = list(userdb._execute(query))
    public_organizers.sort()
    return render_template(
        "public_users.html", title="Public users", public_users=public_organizers
    )
