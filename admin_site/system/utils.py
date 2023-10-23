"""Utility methods for the OS2borgerPC project."""

import json
import logging
import re
import requests
import traceback
from urllib.parse import quote

from importlib import import_module

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.utils import translation
from django.utils.translation import gettext_lazy as _


def notify_users(security_event, security_problem, pc):
    """Notify users about security event."""

    logger = logging.getLogger(__name__)

    # Subject = security name,
    # Body = description + technical summary
    email_list = []
    supervisor_relations = pc.pc_groups.exclude(supervisors=None)
    if supervisor_relations:
        alert_users_pk = list(
            set(supervisor_relations.values_list("supervisors", flat=True))
        )
        alert_users = User.objects.only("email").filter(pk__in=alert_users_pk)
    else:
        alert_users = security_problem.alert_users.only("email").all()
    for user in alert_users:
        email_list.append(user.email)

    body = f"Beskrivelse af sikkerhedsadvarsel: {security_problem.description}\n"
    body += f"Kort resume af data fra log filen : {security_event.summary}"
    try:
        message = EmailMessage(
            f"Sikkerhedsadvarsel for PC : {pc.name}."
            f" Sikkerhedsregel : {security_problem.name}",
            body,
            settings.DEFAULT_FROM_EMAIL,
            email_list,
        )
        message.send(fail_silently=False)
    except Exception:  # Likely Exception: SMTPException
        logger.warning("Security Event e-mail-sending failed:")
        logger.warning(traceback.format_exc())
        return False

    return True


def get_citizen_login_api_validator():
    """Get the function used to validate library user login.

    The validator must take three parameters - username, password and a site
    identity. It will return a unique ID of the authenticated user if
    successful, and something that evaluates to false if unsuccesful.
    """
    path, function = settings.CITIZEN_LOGIN_API_VALIDATOR.rsplit(".", 1)

    module = import_module(path)
    validator = getattr(module, function)

    return validator


def cicero_validate(loaner_number, pincode, site):
    """Do the actual validation against the Cicero service.

    If successful, this function will return the Cicero Patron ID, otherwise it
    will return something falsey like None, 0 or ''.
    """
    logger = logging.getLogger(__name__)

    regex_match = re.fullmatch(f"^\d+$", pincode)
    if not regex_match:
        # logger.warning("Pincode must be a number.")
        return 0
    if not site.isil:
        logger.error("Agency ID / ISIL MUST be specified.")
        return 0
    # First, get sessionKey.
    session_key_url = (
        f"{settings.CICERO_URL}/rest/external/v1/{site.isil}/authentication/login/"
    )
    response = requests.post(
        session_key_url,
        json={
            "username": site.citizen_login_api_user,
            "password": site.citizen_login_api_password,
        },
    )
    if response.ok:
        session_key = response.json()["sessionKey"]
        # Just debugging for the moment.
    else:
        # Unable to authenticate with system user - log this.
        message = response.json()["message"]
        logger.error(
            f"Unable to log in with configured user name and password: {message}"
        )
        return 0
    # We now have a valid session key.
    loaner_auth_url = (
        f"{settings.CICERO_URL}/rest/external/{site.isil}/patrons/authenticate/v6"
    )
    response = requests.post(
        loaner_auth_url,
        headers={"X-session": session_key},
        json={"libraryCardNumber": loaner_number, "pincode": pincode},
    )
    if response.ok:
        result = response.json()
        authenticate_status = result["authenticateStatus"]
        if authenticate_status != "VALID":
            # logger.warning(
            #    f"Unable to authenticate with loaner ID and pin: {authenticate_status}"
            # )
            return 0
        # Loaner has been successfully authenticated.
        patron_id = result["patron"]["patronId"]
        return patron_id


def always_validate_citizen(loaner_number, pincode, agency_id):
    """Perform sanity checks, but always return a suitable patron ID."""
    logger = logging.getLogger(__name__)
    try:
        pincode = int(pincode)
    except ValueError:
        # logger.warning("Pincode must be a number.")
        return 0
    if not agency_id:
        logger.error("Agency ID / ISIL MUST be specified.")
        return 0
    return loaner_number


def get_notification_string(python_list, conjunction="og"):
    """Helper function used to generate human-readable strings
    from python lists."""
    python_list = list(set(python_list))
    if len(python_list) > 1:
        string = ", ".join(python_list[:-1])
        string = " ".join([string, conjunction, python_list[-1]])
    elif len(python_list) == 1:
        string = python_list[0]
    else:
        string = ""
    return string


def set_notification_cookie(response, message, error=False):
    descriptor = {"message": message, "type": "success" if not error else "error"}

    response.set_cookie("page-notification", quote(json.dumps(descriptor), safe=""))


def notification_changes_saved(response, user_profile_language):
    translation.activate(user_profile_language)
    set_notification_cookie(response, _("Changes have been saved %s") % "")
    translation.deactivate()

    return response
