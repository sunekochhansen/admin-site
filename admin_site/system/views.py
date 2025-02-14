# -*- coding: utf-8 -*-
import os
import json
import secrets

from django.http import HttpResponseRedirect, Http404, JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.utils.html import escape
from django.contrib.auth.models import User, Permission
from django.urls import resolve, reverse

from django.views.generic.edit import (
    CreateView,
    DeletionMixin,
    FormView,
    UpdateView,
    DeleteView,
)
from django.views.generic import View, ListView, DetailView, RedirectView, TemplateView
from django.views.generic.list import BaseListView

from django.db import transaction
from django.db.models import Q, F
from django.conf import settings

from django.core.exceptions import PermissionDenied

import django_otp
from two_factor.forms import TOTPDeviceForm
from two_factor.utils import default_device
from two_factor import views as otp_views
from two_factor.plugins.phonenumber.utils import get_available_phone_methods
from django_otp.decorators import otp_required
from django_otp import devices_for_user, user_has_device
from django_otp.plugins.otp_static.models import StaticToken
from django.forms import Form

from system.utils import (
    get_notification_string,
    notification_changes_saved,
    online_pcs_count_filter,
    set_notification_cookie,
)

from account.models import (
    UserProfile,
    SiteMembership,
)

from system.models import (
    APIKey,
    AssociatedScriptParameter,
    ConfigurationEntry,
    Customer,
    ImageVersion,
    Input,
    Job,
    MandatoryParameterMissingError,
    Product,
    PC,
    PCGroup,
    WakeWeekPlan,
    WakeChangeEvent,
    Script,
    ScriptTag,
    SecurityEvent,
    SecurityProblem,
    EventRuleServer,
    EventLevels,
    Site,
    Country,
)

from system.forms import (
    ConfigurationEntryForm,
    EventRuleServerForm,
    PCForm,
    PCGroupForm,
    ParameterForm,
    ScriptForm,
    NewScriptForm,
    SecurityEventForm,
    SiteForm,
    SiteCreateForm,
    UserForm,
    UserLinkForm,
    WakeChangeEventForm,
    WakePlanForm,
)


def run_wake_plan_script(site, pcs, args, user, type="remove"):
    if type == "set":
        script = Script.objects.get(uid="wake_plan_set")
    else:
        script = Script.objects.get(uid="wake_plan_remove")
    script.run_on(site, pcs, *args, user=user)


def otp_check(
    view=None, redirect_field_name="next", login_url=None, if_configured=False
):
    """
    Modfied version of otp_required that redirects to site root if you do not have a device configured
    The normal version redirects to the login page, which results in a loop of logging in,
    hitting a url that requires otp and being redirected back to login
    """
    if login_url is None:
        login_url = "/"

    def test(user):
        return user.is_verified() or (
            if_configured and user.is_authenticated and not user_has_device(user)
        )

    decorator = user_passes_test(
        test, login_url=login_url, redirect_field_name=redirect_field_name
    )

    return decorator if (view is None) else decorator(view)


def site_pcs_stats(context, site_list):
    context["borgerpc_count"] = PC.objects.filter(
        site__in=site_list,
        configuration__entries__key="os2_product",
        configuration__entries__value="os2borgerpc",
    ).count()
    context["borgerpc_kiosk_count"] = PC.objects.filter(
        site__in=site_list,
        configuration__entries__key="os2_product",
        configuration__entries__value="os2borgerpc kiosk",
    ).count()
    # Add counts for each _os_release
    context["releases"] = []
    for release in (
        ConfigurationEntry.objects.filter(key="_os_release")
        .order_by("value")
        .distinct("value")
        .values("value")
    ):
        context["releases"].append(
            (
                release["value"],
                PC.objects.filter(
                    site__in=site_list,
                    configuration__entries__key="_os_release",
                    configuration__entries__value=release["value"],
                ).count(),
            )
        )
    return context


def site_uid_available_check(request):
    uid = request.GET["uid"]
    uid = Site.objects.filter(uid=uid)
    if uid:
        return HttpResponse(
            _("The specified UID is unavailable. Please choose another.")
            + "<script>document.getElementById('create_site_save_button').disabled = true</script>"
        )
    else:
        return HttpResponse(
            "<script>document.getElementById('create_site_save_button').disabled = false</script>"
        )


# Mixin class to require login
class LoginRequiredMixin(View):
    """Subclass in all views where login is required."""

    @method_decorator(login_required)
    def dispatch(self, *args, **kwargs):
        return super(LoginRequiredMixin, self).dispatch(*args, **kwargs)


class SuperAdminOnlyMixin(LoginRequiredMixin):
    """Only allows access to super admins."""

    check_function = user_passes_test(lambda u: u.is_superuser, login_url="/")

    @method_decorator(login_required)
    @method_decorator(check_function)
    def dispatch(self, *args, **kwargs):
        return super(SuperAdminOnlyMixin, self).dispatch(*args, **kwargs)


class SuperAdminOrThisSiteMixin(LoginRequiredMixin):
    @method_decorator(login_required)
    def dispatch(self, *args, **kwargs):
        """Limit access to super users or users belonging to THIS site."""
        site = None
        slug_field = None
        # Check if a site slug is included in the url
        if "slug" in kwargs:
            slug_field = "slug"
        # If none given, give up
        if slug_field:
            site = get_object_or_404(Site, uid=kwargs[slug_field])
        check_function = user_passes_test(
            lambda u: (u.is_superuser) or (site and site in u.user_profile.sites.all()),
            login_url="/",
        )
        wrapped_super = check_function(super(SuperAdminOrThisSiteMixin, self).dispatch)
        return wrapped_super(*args, **kwargs)


# Mixin class for list selection (single select).
class SelectionMixin(View):
    """This supplies the ability to highlight a selected object of a given
    class. This is useful if a Detail view contains a list of children which
    the user is allowed to select."""

    # The Python class of the Django model corresponding to the objects you
    # want to be able to select. MUST be specified in subclass.
    selection_class = None
    # A callable which will return a list of objects which SHOULD belong to the
    # class specified by selection_class. MUST be specified in subclass.
    get_list = None
    # The field which is used to look up the selected object.
    lookup_field = "uid"
    # Overrides the default class name in context.
    class_display_name = None

    def get_context_data(self, **kwargs):
        # First, call superclass
        context = super(SelectionMixin, self).get_context_data(**kwargs)
        # Then get selected object, if any
        if self.lookup_field in self.kwargs:
            lookup_val = self.kwargs[self.lookup_field]
            lookup_params = {self.lookup_field: lookup_val}
            selected = get_object_or_404(self.selection_class, **lookup_params)
        else:
            selected = self.get_list()[0] if self.get_list() else None

        display_name = (
            self.class_display_name
            if self.class_display_name
            else self.selection_class.__name__.lower()
        )
        if selected is not None:
            context["selected_{0}".format(display_name)] = selected
        context["{0}_list".format(display_name)] = self.get_list()
        return context


class JSONResponseMixin:
    """
    A mixin that can be used to render a JSON response.
    """

    def render_to_json_response(self, context, **response_kwargs):
        """
        Returns a JSON response, transforming 'context' to make the payload.
        """
        return JsonResponse(self.get_data(context), **response_kwargs)

    def get_data(self, context):
        """
        Returns an object that will be serialized as JSON by json.dumps().
        """
        # Note: This is *EXTREMELY* naive; in reality, you'll need
        # to do much more complex handling to ensure that arbitrary
        # objects -- such as Django model instances or querysets
        # -- can be serialized as JSON.
        return context


# Mixin class for CRUD views that use site_uid in URL
# The "site_uid" slug is configurable, but please avoid clashes
class SiteMixin(View):
    """Mixin class to extract site UID from URL"""

    def get_context_data(self, **kwargs):
        context = super(SiteMixin, self).get_context_data(**kwargs)
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context["site"] = site
        # Add information about outstanding security events.
        no_of_sec_events = SecurityEvent.objects.priority_events_for_site(site).count()
        context["sec_events"] = no_of_sec_events

        return context


# Main index/site root view
class AdminIndex(RedirectView, LoginRequiredMixin):
    """Redirects to admin overview (sites list) or site main page."""

    def get_redirect_url(self, **kwargs):
        """Redirect based on user. This view will use the RequireLogin mixin,
        so we'll always have a logged-in user."""
        user = self.request.user
        profile = user.user_profile

        # If user only has one site, redirect to that.
        if profile.sites.count() == 1:
            site = profile.sites.first()
            return reverse("site", kwargs={"slug": site.url})
        # In all other cases we can redirect to list of sites.
        return reverse("sites")


class SiteList(ListView, LoginRequiredMixin):
    """
    Site overview.

    Provides a list of sites a user has access to.
    """

    model = Site
    context_object_name = "site_list"
    template_name = "system/sites/list.html"

    def get_queryset(self):
        user = self.request.user
        if (
            not user.is_superuser
            and not user.user_profile.sites.count() > 1
            and not user.user_profile.sitemembership_set.first().site_user_type
            == SiteMembership.CUSTOMER_ADMIN
        ):
            raise PermissionDenied
        if user.is_superuser:
            qs = Site.objects.all()
        else:
            qs = user.user_profile.sites.all()

        return qs

    def get_context_data(self, **kwargs):
        context = super(SiteList, self).get_context_data(**kwargs)
        context = site_pcs_stats(context, self.get_queryset())
        total_pcs = PC.objects.filter(site__in=self.get_queryset())
        context["total_pcs_count"] = len(total_pcs)
        context["total_activated_pcs_count"] = total_pcs.filter(
            is_activated=True
        ).count()
        context["total_online_pcs_count"] = online_pcs_count_filter(total_pcs)
        context["user"] = self.request.user
        context["site_membership"] = (
            self.request.user.user_profile.sitemembership_set.order_by(
                "site_user_type"
            ).last()
        )
        context["version"] = open("/code/VERSION", "r").read()
        user_sites = self.get_queryset()
        context["user_sites"] = user_sites
        # The dictionary to generate the customer-site list has the following structure:
        # {"Denmark": [Customer1, Customer2], "Sweden": [Customer3, ...] ...}
        # Handling the logic for non-superusers differently because it can be done in a much less complex way
        if self.request.user.is_superuser:
            countries = Country.objects.all()
        else:
            countries = Country.objects.filter(
                id__in=user_sites.values_list("customer__country", flat=True)
            )

        countries_dict = {}
        for country in countries:
            customers = Customer.objects.filter(
                country=country, id__in=user_sites.values_list("customer", flat=True)
            )
            countries_dict[country.name] = customers
        context["countries_dict"] = countries_dict
        context["form"] = SiteCreateForm()
        return context


class SiteCreate(CreateView, LoginRequiredMixin):
    model = Site
    form_class = SiteCreateForm

    def form_valid(self, form):
        # Only allow customer admins to use this functionality
        if self.request.user.user_profile.sitemembership_set.filter(
            site_user_type=SiteMembership.CUSTOMER_ADMIN
        ):
            self.object = form.save(commit=False)
            # This doesn't seem totally ideal. Maybe if user or user_profile had a direct relation to customer, or??
            customer = (
                self.request.user.user_profile.sitemembership_set.filter(
                    site_user_type=SiteMembership.CUSTOMER_ADMIN
                )
                .first()
                .site.customer
            )
            self.object.customer = customer

            response = super(SiteCreate, self).form_valid(form)

            # Ensure that all customer admins for the customer have access to the new Site
            customer_admins_for_customer = list(
                set(
                    UserProfile.objects.filter(
                        sites__in=customer.sites.all(),
                        sitemembership__site_user_type=SiteMembership.CUSTOMER_ADMIN,
                    )
                )
            )
            for customer_admin in customer_admins_for_customer:
                SiteMembership.objects.create(
                    user_profile=customer_admin,
                    site=self.object,
                    site_user_type=SiteMembership.CUSTOMER_ADMIN,
                )

            set_notification_cookie(response, _("Site %s created") % self.object.name)

            return response
        else:
            raise PermissionDenied

    def form_invalid(self, form):
        response = HttpResponseRedirect(reverse("sites"))

        set_notification_cookie(
            response,
            _(
                "The Site could not be created because the chosen UID "
                "%s was invalid or not unique"
            )
            % form.data["uid"],
            error=True,
        )

        return response

    def get_success_url(self):
        return reverse("sites")


class SiteDelete(DeleteView, SuperAdminOrThisSiteMixin):
    model = Site
    template_name = "system/sites/confirm_delete.html"

    def get(self, request, *args, **kwargs):
        """
        Overwrite the get method to ensure that customer admins
        can't directly access the delete URL for sites with
        5 or more computers. We do it this way to avoid
        using PermissionDenied, which might confuse
        some customers since customer admins do have
        permission to delete sites.
        """
        # Call the super-method first so non-customer admins
        # are shown the proper PermissionDenied
        response = super().get(request, *args, **kwargs)
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        # If the site has 5 or more computers, redirect away
        # from this view.
        # Also don't let them delete their last site
        if site.pcs.count() > 4 or site.customer.sites.count() == 1:
            return redirect("/")
        return response

    def get_object(self, queryset=None):
        self.selected_site = get_object_or_404(Site, uid=self.kwargs["slug"])

        # Only customer admins are allowed to access this view
        if (
            not self.request.user.is_superuser
            and self.request.user.user_profile.sitemembership_set.get(
                site=self.selected_site
            ).site_user_type
            != SiteMembership.CUSTOMER_ADMIN
        ):
            raise PermissionDenied

        return self.selected_site

    def get_context_data(self, **kwargs):
        context = super(SiteDelete, self).get_context_data(**kwargs)
        context["selected_site"] = self.selected_site

        return context

    def get_success_url(self):
        return reverse("sites")

    def form_valid(self, form, *args, **kwargs):
        if (
            (
                not self.request.user.is_superuser
                and not self.request.user.user_profile.sitemembership_set.filter(
                    site_user_type=SiteMembership.CUSTOMER_ADMIN
                )
            )
            or self.selected_site.pcs.count() > 4
            or self.selected_site.customer.sites.count() == 1
        ):
            # You can only get here by deliberately trying to circumvent the system,
            # so we don't care about possibly showing PermissionDenied to a customer admin
            raise PermissionDenied
        # Delete any users that only existed on this site
        for user in self.selected_site.users:
            if len(user.user_profile.sitemembership_set.all()) == 1:
                user.delete()
        site_name = self.selected_site.name
        response = super(SiteDelete, self).delete(form, *args, **kwargs)
        set_notification_cookie(response, _("Site %s deleted") % site_name)

        return response


# Base class for Site-based passive (non-form) views
class SiteView(DetailView, SuperAdminOrThisSiteMixin):
    """Base class for all views based on a single site."""

    model = Site
    slug_field = "uid"

    def get_context_data(self, **kwargs):
        context = super(SiteView, self).get_context_data(**kwargs)
        site = self.get_object()
        # Add information about outstanding security events.
        no_of_sec_events = SecurityEvent.objects.priority_events_for_site(site).count()
        context["sec_events"] = no_of_sec_events

        return context


class SiteDetailView(SiteView):
    """Class for showing the overview that is displayed when entering a site"""

    template_name = "system/site_status.html"

    # For hver pc skal vi hente seneste security event.
    def get_context_data(self, **kwargs):
        context = super(SiteDetailView, self).get_context_data(**kwargs)
        context = site_pcs_stats(context, [kwargs["object"]])

        site_pcs = self.object.pcs.all()

        # Top level list of new PCs etc.
        context["ls_pcs"] = site_pcs.order_by(
            "is_activated", F("last_seen").desc(nulls_last=True)
        )

        context["total_pcs_count"] = context["ls_pcs"].count()
        context["activated_pcs_count"] = site_pcs.filter(is_activated=True).count()
        context["online_pcs_count"] = online_pcs_count_filter(site_pcs)

        return context


class SiteSettings(UpdateView, SiteView):
    form_class = SiteForm
    template_name = "system/site_settings/site_settings.html"

    def get_context_data(self, **kwargs):
        # First, get basic context from superclass
        context = super(SiteSettings, self).get_context_data(**kwargs)
        context["site_configs"] = self.object.configuration.entries.all()

        return context

    def form_valid(self, form):
        # Only overwrite login API password if the form input for it was non-empty
        if not form.cleaned_data["citizen_login_api_password"]:
            site = get_object_or_404(Site, uid=self.kwargs["slug"])
            form.instance.citizen_login_api_password = site.citizen_login_api_password
        # Only overwrite the Easy!Appointments API key if the form input for it was non-empty
        if not form.cleaned_data["booking_api_key"]:
            site = get_object_or_404(Site, uid=self.kwargs["slug"])
            form.instance.booking_api_key = site.booking_api_key
        # Only overwrite the login API key if the form input for it was non-empty
        if not form.cleaned_data["citizen_login_api_key"]:
            site = get_object_or_404(Site, uid=self.kwargs["slug"])
            form.instance.citizen_login_api_key = site.citizen_login_api_key

        self.object.configuration.update_from_request(self.request.POST, "site_configs")

        response = super(SiteSettings, self).form_valid(form)

        set_notification_cookie(
            response, _("Settings for %s updated") % self.kwargs["slug"]
        )
        return response


class TwoFactor(SiteView, SiteMixin):
    template_name = "system/site_two_factor_pc.html"


class APIKeyUpdate(UpdateView, SiteView, DeletionMixin):
    # form_class = ?
    template_name = "system/site_settings/api_keys/api_keys.html"
    fields = "__all__"

    def get_context_data(self, **kwargs):
        # First, get basic context from superclass
        context = super(APIKeyUpdate, self).get_context_data(**kwargs)

        context["api_keys"] = self.object.apikeys.all()

        return context

    # def form_valid(self, form):
    def post(self, request, *args, **kwargs):
        new_description = request.POST["description"]

        APIKey.objects.filter(id=kwargs["pk"]).update(description=new_description)

        return HttpResponse("OK")


class APIKeyCreate(CreateView, SuperAdminOrThisSiteMixin):
    model = APIKey
    fields = "__all__"
    template_name = "system/site_settings/api_keys/partials/list.html"

    # TODO: Consider making a common class they inherit from, to not duplicate get_context_data (and maybe other view functions)
    def get_context_data(self, **kwargs):
        # First, get basic context from superclass
        context = super(APIKeyCreate, self).get_context_data(**kwargs)

        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context["api_keys"] = APIKey.objects.filter(site=site)

        return context

    # def form_valid(self, form):
    def post(self, request, *args, **kwargs):
        # Do basic method
        # kwargs["updated"] = True
        response = self.get(request, *args, **kwargs)

        # Handle saving of data
        super(APIKeyCreate, self).post(request, *args, **kwargs)

        # Generate an API Key
        KEY_LENGTH = 75
        key = secrets.token_urlsafe(KEY_LENGTH)
        while APIKey.objects.filter(key=key).count() > 0:
            key = secrets.token_urlsafe(KEY_LENGTH)

        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        APIKey.objects.create(key=key, site=site)

        return response


# class APIKeyDelete(DeleteView, SuperAdminOrThisSiteMixin):
class APIKeyDelete(TemplateView, DeletionMixin, SuperAdminOrThisSiteMixin):
    model = APIKey
    template_name = "system/site_settings/api_keys/partials/list.html"

    # TODO: Consider making a common class they inherit from, to not duplicate get_context_data (and maybe other view functions)
    def get_context_data(self, **kwargs):
        # First, get basic context from superclass
        context = super().get_context_data(**kwargs)

        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context["api_keys"] = APIKey.objects.filter(site=site)

        return context

    def delete(self, request, *args, **kwargs):
        APIKey.objects.get(id=kwargs["pk"]).delete()

        return render(
            request,
            "system/site_settings/api_keys/partials/list.html",
            self.get_context_data(),
        )


class AdminTwoFactorDisable(otp_views.DisableView, SuperAdminOrThisSiteMixin):
    form_class = Form

    def get_success_url(self):
        return reverse(
            "user",
            kwargs={
                "slug": self.kwargs["slug"],
                "username": self.kwargs["username"],
            },
        )

    def get_context_data(self, **kwargs):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context = {"site": site, "user": self.request.user, "form": Form}
        return context

    def dispatch(self, *args, **kwargs):
        """This function has been overwritten to make it use get_success_url
        and to redirect when the username does not match"""
        # If the username in the url doesn't match request.user.username,
        # redirect back to the main site
        if self.request.user.username != self.kwargs["username"]:
            return redirect("/")
        fn = otp_required(
            super().dispatch, login_url=self.get_success_url(), redirect_field_name=None
        )
        return fn(*args, **kwargs)

    def form_valid(self, form):
        """This function has been overwritten to make it use get_success_url"""
        for device in devices_for_user(self.request.user):
            device.delete()
        return redirect(self.get_success_url())


class AdminTwoFactorSetup(otp_views.SetupView, SuperAdminOrThisSiteMixin):
    def get_success_url(self):
        return reverse(
            "admin_otp_setup_complete",
            kwargs={"slug": self.kwargs["slug"], "username": self.kwargs["username"]},
        )

    def get_context_data(self, form, **kwargs):
        context = super().get_context_data(form, **kwargs)
        user = self.request.user
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context["site"] = site
        # url to redirect to when the user clicks cancel
        context["cancel_url"] = reverse("users", kwargs={"slug": site.uid})
        return context

    def get(self, request, *args, **kwargs):
        """
        Start the setup wizard. Redirect if already enabled.
        This function has been overwritten in order to redirect
        when the username does not match
        """
        # If the username in the url doesn't match request.user.username,
        # redirect back to the main site
        if self.request.user.username != self.kwargs["username"]:
            return redirect("/")
        elif default_device(self.request.user):
            return redirect(self.get_success_url())
        return super().get(request, *args, **kwargs)

    def done(self, form_list, **kwargs):
        """
        Finish the wizard. Save all forms and redirect.
        This function has been overwritten to make it
        use get_success_url in the final redirect.
        All other lines are unchanged.
        """
        # Remove secret key used for QR code generation
        try:
            del self.request.session[self.session_key_name]
        except KeyError:
            pass

        method = self.get_method()
        # TOTPDeviceForm
        if method.code == "generator":
            form = [form for form in form_list if isinstance(form, TOTPDeviceForm)][0]
            device = form.save()

        # PhoneNumberForm / YubiKeyDeviceForm / EmailForm / WebauthnDeviceValidationForm
        elif method.code in ("call", "sms", "yubikey", "email", "webauthn"):
            device = self.get_device()
            device.save()

        else:
            raise NotImplementedError("Unknown method '%s'" % method.code)

        django_otp.login(self.request, device)
        return redirect(self.get_success_url())


@method_decorator(otp_check, name="dispatch")
class AdminTwoFactorSetupComplete(
    otp_views.SetupCompleteView, SuperAdminOrThisSiteMixin
):
    def get_context_data(self, **kwargs):
        context = {
            "phone_methods": get_available_phone_methods(),
        }
        user = self.request.user
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context["site"] = site
        context["user"] = user
        return context

    def dispatch(self, request, *args, **kwargs):
        # Override the dispatch method in order to redirect to site root
        # if the url username does not match request.user.username
        if request.user.username != kwargs["username"]:
            return redirect("/")
        # Everything below this point is unchanged from the
        # standard django View dispatch
        if request.method.lower() in self.http_method_names:
            handler = getattr(
                self, request.method.lower(), self.http_method_not_allowed
            )
        else:
            handler = self.http_method_not_allowed
        return handler(request, *args, **kwargs)


@method_decorator(otp_check, name="dispatch")
class AdminTwoFactorBackupTokens(otp_views.BackupTokensView, SuperAdminOrThisSiteMixin):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["user"] = self.request.user
        context["site"] = get_object_or_404(Site, uid=self.kwargs["slug"])
        return context

    def dispatch(self, request, *args, **kwargs):
        # Override the dispatch method in order to redirect to site root
        # if the url username does not match request.user.username
        if request.user.username != kwargs["username"]:
            return redirect("/")
        # Everything below this point is unchanged from the
        # standard django View dispatch
        if request.method.lower() in self.http_method_names:
            handler = getattr(
                self, request.method.lower(), self.http_method_not_allowed
            )
        else:
            handler = self.http_method_not_allowed
        return handler(request, *args, **kwargs)

    def form_valid(self, form):
        """
        Delete existing backup codes and generate new ones.
        This function has been overwritten in order to change success_url
        """
        device = self.get_device()
        device.token_set.all().delete()
        for n in range(self.number_of_tokens):
            device.token_set.create(token=StaticToken.random_token())

        # Stay on this page after generating new backup tokens
        success_url = reverse("admin_otp_backup", kwargs=self.kwargs)

        return redirect(success_url)


# Now follows all site-based views, i.e. subclasses of SiteView.
class JobsView(SiteView):
    template_name = "system/jobs/site_jobs.html"

    def get_context_data(self, **kwargs):
        # First, get basic context from superclass
        context = super(JobsView, self).get_context_data(**kwargs)
        site = context["site"]
        context["batches"] = site.batches.exclude(name="")[:100]
        context["pcs"] = site.pcs.all()
        context["groups"] = site.groups.all()
        preselected = set(
            [
                Job.NEW,
                Job.SUBMITTED,
                Job.FAILED,
                Job.DONE,
            ]
        )
        context["status_choices"] = [
            {
                "name": name,
                "value": value,
                "label": Job.STATUS_TO_LABEL[value],
                "checked": 'checked="checked' if value in preselected else "",
            }
            for (value, name) in Job.STATUS_CHOICES
        ]
        params = self.request.GET or self.request.POST

        for k in ["batch", "pc", "group"]:
            v = params.get(k, None)
            if v is not None and v.isdigit():
                context["selected_%s" % k] = int(v)

        return context


class JobSearch(SiteMixin, JSONResponseMixin, BaseListView, SuperAdminOrThisSiteMixin):
    paginate_by = 20
    http_method_names = ["get"]
    VALID_ORDER_BY = []
    for i in [
        "pk",
        "batch__script__name",
        "created",
        "started",
        "finished",
        "status",
        "pc__name",
        "batch__name",
        "user__username",
    ]:
        VALID_ORDER_BY.append(i)
        VALID_ORDER_BY.append("-" + i)

    context_object_name = "jobs_list"

    def render_to_response(self, context, **response_kwargs):
        return self.render_to_json_response(context, **response_kwargs)

    def get_queryset(self):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        if not self.request.user.is_superuser:
            queryset = Job.objects.filter(
                Q(batch__script__is_hidden=False)
                | Q(
                    batch__script__feature_permission__in=site.customer.feature_permission.all()
                )
            )
        else:
            queryset = Job.objects.all()
        params = self.request.GET

        query = {"batch__site": site}

        if "status" in params:
            query["status__in"] = params.getlist("status")

        for k in ["pc", "batch"]:
            v = params.get(k, "")
            if v != "":
                query[k] = v

        group = params.get("group", "")
        if group != "":
            query["pc__pc_groups"] = group

        orderby = params.get("orderby", "-pk")
        if orderby not in JobSearch.VALID_ORDER_BY:
            orderby = "-pk"

        queryset = queryset.filter(**query).order_by(orderby, "pk")

        return queryset

    # for admin users the user_url is a redirect to our job docs
    # explaining scripts run as "Magenta"
    def get_username(self, user):
        if user:
            if user and user.is_superuser:
                return "Admin"
            else:
                return user.username
        else:
            return ""

    def get_user_url(self, user, uid):
        if user:
            if user.is_superuser:
                return reverse("doc", kwargs={"name": "jobs"})
            else:
                return (reverse("user", args=[uid, user.username]),)
        else:
            return ""

    def get_data(self, context):
        site = context["site"]
        page_obj = context["page_obj"]
        paginator = context["paginator"]
        adjacent_pages = 2
        page_numbers = [
            n
            for n in range(
                page_obj.number - adjacent_pages, page_obj.number + adjacent_pages + 1
            )
            if n > 0 and n <= paginator.num_pages
        ]

        page = {
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "page": page_obj.number,
            "page_numbers": page_numbers,
            "has_next": page_obj.has_next(),
            "next_page_number": (
                page_obj.next_page_number() if page_obj.has_next() else None
            ),
            "has_previous": page_obj.has_previous(),
            "previous_page_number": (
                page_obj.previous_page_number() if page_obj.has_previous() else None
            ),
            "results": [
                {
                    "pk": job.pk,
                    "script_name": job.batch.script.name,
                    "started": (
                        job.started.strftime("%Y-%m-%d %H:%M:%S")
                        if job.started
                        else "-"
                    ),
                    "finished": (
                        job.finished.strftime("%Y-%m-%d %H:%M:%S")
                        if job.finished
                        else "-"
                    ),
                    "created": (
                        job.created.strftime("%Y-%m-%d %H:%M:%S")
                        if job.created
                        else "-"
                    ),
                    "status": job.status_translated + "",
                    "label": job.status_label,
                    "pc_name": job.pc.name,
                    "batch_name": job.batch.name,
                    "user": self.get_username(job.user),
                    "user_url": self.get_user_url(job.user, site.uid),
                    "has_info": job.has_info,
                    "script_url": reverse(
                        "script", args=[site.uid, job.batch.script.id]
                    ),
                    "pc_url": reverse("computer", args=[site.uid, job.pc.uid]),
                    "restart_url": reverse("restart_job", args=[site.uid, job.pk]),
                }
                for job in page_obj
            ],
        }

        return page


class JobRestarter(DetailView, SuperAdminOrThisSiteMixin):
    template_name = "system/jobs/restart.html"
    model = Job

    def status_fail_response(self):
        response = HttpResponseRedirect(self.get_success_url())
        set_notification_cookie(
            response,
            _("Can only restart jobs that are Done or Failed %s") % "",
        )
        return response

    def get(self, request, *args, **kwargs):
        self.site = get_object_or_404(Site, uid=kwargs["slug"])
        self.object = self.get_object()

        # Only restart jobs that have failed or succeeded
        if not self.object.finished:
            return self.status_fail_response()

        context = self.get_context_data(object=self.object)

        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super(JobRestarter, self).get_context_data(**kwargs)
        context["site"] = self.site
        context["selected_job"] = self.object
        return context

    def post(self, request, *args, **kwargs):
        self.site = get_object_or_404(Site, uid=kwargs["slug"])
        self.object = self.get_object()

        if not self.object.finished:
            return self.status_fail_response()

        self.object.restart(user=self.request.user)
        response = HttpResponseRedirect(self.get_success_url())
        set_notification_cookie(
            response,
            _("The script %s is being rerun on the computer %s")
            % (self.object.batch.script.name, self.object.pc.name),
        )
        return response

    def get_success_url(self):
        return reverse("jobs", kwargs={"slug": self.kwargs["slug"]})


class JobInfo(DetailView, SuperAdminOrThisSiteMixin):
    template_name = "system/jobs/info.html"
    model = Job

    def get(self, request, *args, **kwargs):
        self.site = get_object_or_404(Site, uid=kwargs["slug"])
        return super(JobInfo, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(JobInfo, self).get_context_data(**kwargs)
        if self.site != self.object.batch.site:
            raise Http404
        context["site"] = self.site
        context["job"] = self.object
        return context


class ScriptMixin(object):
    script = None
    script_inputs = ""
    is_security = False

    def setup_script_editing(self, **kwargs):
        # Get site
        self.site = get_object_or_404(Site, uid=kwargs["slug"])
        # Add the global and local script lists
        self.scripts = Script.objects.filter(
            Q(site=self.site) | Q(site=None), is_security_script=self.is_security
        )

        if "script_pk" in kwargs:
            self.script = get_object_or_404(Script, pk=kwargs["script_pk"])
            if self.script.site and self.script.site != self.site:
                raise Http404(
                    _("You have no Script with the following ID: %s")
                    % self.kwargs["script_pk"]
                )

    def get(self, request, *args, **kwargs):
        self.setup_script_editing(**kwargs)
        return super(ScriptMixin, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.setup_script_editing(**kwargs)
        return super(ScriptMixin, self).post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        # Get context from super class
        context = super(ScriptMixin, self).get_context_data(**kwargs)
        context["site"] = self.site
        context["script_tags"] = ScriptTag.objects.all()

        scripts = self.scripts.filter(is_hidden=False)

        # Append scripts the site has permissions for
        for fp in context["site"].customer.feature_permission.all():
            scripts = scripts | fp.scripts.filter(is_security_script=self.is_security)

        local_scripts = scripts.filter(site=self.site)
        context["local_scripts"] = local_scripts
        global_scripts = scripts.filter(site=None)
        context["global_scripts"] = global_scripts

        if self.script:
            context["supported_products"] = self.script.products.all()

        # Create a tag->scripts dict for tags that has local scripts.
        local_tag_scripts_dict = {
            tag: local_scripts.filter(tags=tag)
            for tag in ScriptTag.objects.all()
            if local_scripts.filter(tags=tag).exists()
        }
        # Add scripts with no tags as untagged.
        if local_scripts.filter(tags=None).exists():
            local_tag_scripts_dict["untagged"] = local_scripts.filter(tags=None)

        context["local_scripts_by_tag"] = local_tag_scripts_dict

        # Create a tag->scripts dict for tags that has global scripts.
        global_tag_scripts_dict = {
            tag: global_scripts.filter(tags=tag)
            for tag in ScriptTag.objects.all()
            if global_scripts.filter(tags=tag).exists()
        }
        # Add scripts with no tags as untagged.
        if global_scripts.filter(tags=None).exists():
            global_tag_scripts_dict["untagged"] = global_scripts.filter(tags=None)

        context["global_scripts_by_tag"] = global_tag_scripts_dict

        context["script_inputs"] = self.script_inputs
        context["is_security"] = self.is_security
        if self.is_security:
            context["script_url"] = "security_script"
        else:
            context["script_url"] = "script"

        # If we selected a script add it to context
        if self.script is not None:
            context["selected_script"] = self.script
            if self.script.site is None:
                context["global_selected"] = True
            if not context["script_inputs"]:
                context["script_inputs"] = [
                    {
                        "pk": input.pk,
                        "name": input.name.replace('"', "&quot;"),
                        "value_type": input.value_type,
                        "default_value": input.default_value,
                        "mandatory": input.mandatory,
                    }
                    for input in self.script.ordered_inputs
                ]
        elif not context["script_inputs"]:
            context["script_inputs"] = []

        context["script_inputs_json"] = json.dumps(context["script_inputs"])
        # Add information about outstanding security events.
        no_of_sec_events = SecurityEvent.objects.priority_events_for_site(
            self.site
        ).count()
        context["sec_events"] = no_of_sec_events

        return context

    def validate_script_inputs(self):
        params = self.request.POST
        num_inputs = params.get("script-number-of-inputs", 0)
        inputs = []
        success = True
        if int(num_inputs) > 0:
            for i in range(int(num_inputs)):
                data = {
                    "pk": params.get("script-input-%d-pk" % i, None),
                    "name": params.get("script-input-%d-name" % i, ""),
                    "value_type": params.get("script-input-%d-type" % i, ""),
                    "position": i,
                    "default_value": params.get("script-input-%d-default" % i, ""),
                    "mandatory": params.get(
                        "script-input-%d-mandatory" % i, "unchecked"
                    ),
                }

                if data["name"] is None or data["name"] == "":
                    data["name_error"] = _("Error: You must provide a name")
                    success = False

                if data["value_type"] not in [
                    value for (value, name) in Input.VALUE_CHOICES
                ]:
                    data["type_error"] = _(
                        "Error: You must provide a correct input parameter type"
                    )
                    success = False

                data["mandatory"] = data["mandatory"] != "unchecked"

                inputs.append(data)

            self.script_inputs = inputs

        return success

    def save_script_inputs(self):
        # First delete the existing inputs not found in the new inputs.
        pks = [
            script_input.get("pk")
            for script_input in self.script_inputs
            if script_input.get("pk")
        ]
        self.script.inputs.exclude(pk__in=pks).delete()

        for input_data in self.script_inputs:
            input_data["script"] = self.script

            if "pk" in input_data and not input_data["pk"]:
                del input_data["pk"]

            Input.objects.update_or_create(pk=input_data.get("pk"), defaults=input_data)

    def create_associated_script_parameters(self):
        for associated_script in self.script.associations.all():
            for script_input in self.script.ordered_inputs:
                par = AssociatedScriptParameter.objects.filter(
                    associated_script=associated_script, input=script_input
                ).first()
                if not par:
                    par = AssociatedScriptParameter(
                        associated_script=associated_script, input=script_input
                    )
                    if script_input.value_type == Input.BOOLEAN:
                        par.string_value = "True"
                    par.save()


class ScriptRedirect(RedirectView, SuperAdminOrThisSiteMixin):
    def get_redirect_url(self, **kwargs):
        site = get_object_or_404(Site, uid=kwargs["slug"])
        is_security = (
            True if resolve(self.request.path).url_name == "security_scripts" else False
        )

        # Scripts are sorted with "-site" to ensure global scripts are ordered first in the queryset.
        scripts = Script.objects.filter(
            Q(site=site) | Q(site=None), is_security_script=is_security, is_hidden=False
        ).order_by("-site", "name")

        if scripts.exists():
            script = scripts.first()
            return script.get_absolute_url(slug=site.uid)
        else:
            return (
                reverse("new_security_script", args=[site.uid])
                if is_security
                else reverse("new_script", args=[site.uid])
            )


class ScriptCreate(ScriptMixin, CreateView, SuperAdminOrThisSiteMixin):
    template_name = "system/scripts/create.html"
    form_class = ScriptForm

    def get_context_data(self, **kwargs):
        context = super(ScriptCreate, self).get_context_data(**kwargs)
        context["type_choices"] = Input.VALUE_CHOICES
        return context

    def get_form(self, form_class=None):
        if form_class is None:
            form_class = self.get_form_class()
        form = super(ScriptCreate, self).get_form(form_class)
        form.prefix = "create"
        return form

    def form_valid(self, form):
        if self.validate_script_inputs():
            # save the username for the AuditModelMixin.
            form.instance.user_created = self.request.user.username
            self.object = form.save()
            self.script = self.object
            if self.is_security:
                self.object.is_security_script = True
                self.object.save()
            self.save_script_inputs()
            return HttpResponseRedirect(self.get_success_url())
        else:
            return self.form_invalid(form, transfer_inputs=False)

    def form_invalid(self, form, transfer_inputs=True):
        if transfer_inputs:
            self.validate_script_inputs()

        return super(ScriptCreate, self).form_invalid(form)

    def get_success_url(self):
        if self.is_security:
            return reverse("security_script", args=[self.site.uid, self.script.pk])
        else:
            return reverse("script", args=[self.site.uid, self.script.pk])


class ScriptUpdate(ScriptMixin, UpdateView, SuperAdminOrThisSiteMixin):
    template_name = "system/scripts/update.html"
    form_class = ScriptForm

    # This get_form method is overriden to pass global_script to the form_class (ScripForm) instance
    def get_form(self, form_class=None):
        if form_class is None:
            form_class = self.get_form_class()

        script_instance = self.get_object()
        global_script = script_instance.is_global if script_instance else False

        # Ensure the original arguments are passed to the form, along with global_script
        form_kwargs = self.get_form_kwargs()
        form_kwargs["global_script"] = global_script

        return form_class(**form_kwargs)

    def get_context_data(self, **kwargs):
        # Get context from super class
        context = super(ScriptUpdate, self).get_context_data(**kwargs)
        if self.script is not None and self.script.executable_code is not None:
            try:
                display_code = self.script.executable_code.read().decode("utf-8")
            except UnicodeDecodeError:
                display_code = "<Kan ikke vise koden - binære data.>"
            except FileNotFoundError:
                display_code = "<Kan ikke vise koden - upload venligst igen.>"
            context["script_preview"] = display_code
        context["type_choices"] = Input.VALUE_CHOICES
        self.create_form = NewScriptForm()
        self.create_form.prefix = "create"
        context["create_form"] = self.create_form
        context["is_hidden"] = self.script.is_hidden
        if self.script.uid:
            context["uid"] = self.script.uid
        request_user = self.request.user
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context["site_membership"] = (
            request_user.user_profile.sitemembership_set.filter(site_id=site.id).first()
        )
        return context

    def get_object(self, queryset=None):
        if (
            self.script.is_hidden
            and not self.request.user.is_superuser
            and not (
                self.script.feature_permission
                in self.site.customer.feature_permission.all()
            )
        ):
            raise PermissionDenied
        return self.script

    def form_valid(self, form):
        if self.validate_script_inputs():
            # save the username for the AuditModelMixin.
            form.instance.user_modified = self.request.user.username
            self.save_script_inputs()
            self.create_associated_script_parameters()
            response = super(ScriptUpdate, self).form_valid(form)
            set_notification_cookie(response, _("Script %s updated") % self.script.name)

            return response
        else:
            return self.form_invalid(form, transfer_inputs=False)

    def form_invalid(self, form, transfer_inputs=True):
        if transfer_inputs:
            self.validate_script_inputs()

        return super(ScriptUpdate, self).form_invalid(form)

    def get_success_url(self):
        if self.is_security:
            return reverse("security_script", args=[self.site.uid, self.script.pk])
        else:
            return reverse("script", args=[self.site.uid, self.script.pk])


class GlobalScriptRedirect(RedirectView, LoginRequiredMixin):
    permanent = False
    query_string = True

    def get_redirect_url(self, *args, **kwargs):
        user = self.request.user

        if "script_pk" in kwargs:
            script = get_object_or_404(Script, id=kwargs["script_pk"])
        else:
            script = get_object_or_404(Script, uid=kwargs["script_uid"])

        # No need to support this for local scripts
        if script.site:
            return "/"
        else:  # If the script is global
            # If a user is a member of multiple sites, just randomly send them to the first one
            first_slug = user.user_profile.sites.all().first().uid

            if script.is_security_script:
                return reverse("security_script", args=[first_slug, script.pk])
            else:
                return reverse("script", args=[first_slug, script.pk])


class ScriptRun(SiteView):
    action = None
    form = None
    STEP1 = "choose_pcs_and_groups"
    STEP2 = "choose_parameters"
    STEP3 = "run_script"

    def post(self, request, *args, **kwargs):
        return super(ScriptRun, self).get(request, *args, **kwargs)

    def fetch_pcs_from_request(self):
        # Transfer chosen groups and PCs as PC pks
        pcs = [int(pk) for pk in self.request.POST.getlist("pcs", [])]
        for group_pk in self.request.POST.getlist("groups", []):
            group = PCGroup.objects.get(pk=group_pk)
            for pc in group.pcs.all():
                pcs.append(int(pc.pk))
        # Uniquify
        selected_pcs_groups_set = list(set(pcs))
        return (selected_pcs_groups_set, len(selected_pcs_groups_set))

    def step1(self, context):
        self.template_name = "system/scripts/run_step1.html"
        context["pcs"] = self.object.pcs.all()
        all_groups = self.object.groups.all()
        context["groups"] = [group for group in all_groups if group.pcs.count() > 0]

        if len(context["script"].ordered_inputs) > 0:
            context["action"] = ScriptRun.STEP2
        else:
            context["action"] = ScriptRun.STEP3

    def step2(self, context):
        self.template_name = "system/scripts/run_step2.html"

        context["pcs"], context["num_pcs"] = self.fetch_pcs_from_request()
        if context["num_pcs"] == 0:
            context["message"] = _("You must specify at least one group or pc")
            self.step1(context)
            return

        # Set up the form
        if "form" not in context:
            context["form"] = ParameterForm(script=context["script"])

        # Go to step3 on submit
        context["action"] = ScriptRun.STEP3

    def step3(self, context):
        self.template_name = "system/scripts/run_step3.html"
        form = ParameterForm(
            self.request.POST, self.request.FILES, script=context["script"]
        )
        context["form"] = form

        # When run in step 3 and step 2 wasn't bypassed, don't do this calculation again
        if "selected_pcs" not in context:
            context["selected_pcs"], context["num_pcs"] = self.fetch_pcs_from_request()
        if context["num_pcs"] == 0:
            context["message"] = _("You must specify at least one group or pc")
            self.step1(context)
            return

        if not form.is_valid():
            self.step2(context)
        else:
            args = []
            for i in range(0, context["script"].inputs.count()):
                # Non-mandatory Integer and Date fields send "None", which causes an IntegrityError since string_value isn't null=True
                args.append(
                    ""
                    if form.cleaned_data[f"parameter_{i}"] is None
                    else form.cleaned_data[f"parameter_{i}"]
                )

            context["batch"] = context["script"].run_on(
                context["site"],
                PC.objects.filter(pk__in=context["selected_pcs"]),
                *args,
                user=self.request.user,
            )

    def get_context_data(self, **kwargs):
        context = super(ScriptRun, self).get_context_data(**kwargs)
        context["script"] = get_object_or_404(Script, pk=self.kwargs["script_pk"])

        action = self.request.POST.get("action", "choose_pcs_and_groups")
        if action == ScriptRun.STEP1:
            self.step1(context)
        elif action == ScriptRun.STEP2:
            self.step2(context)
        elif action == ScriptRun.STEP3:
            self.step3(context)
        else:
            raise Exception("POST to ScriptRun with wrong action %s" % self.action)

        return context


class ScriptDelete(ScriptMixin, SuperAdminOrThisSiteMixin, DeleteView):
    template_name = "system/scripts/confirm_delete.html"
    model = Script

    def get_object(self, queryset=None):
        return Script.objects.get(
            pk=self.kwargs["script_pk"], site__uid=self.kwargs["slug"]
        )

    def get_success_url(self):
        if self.is_security:
            return reverse("security_scripts", kwargs={"slug": self.kwargs["slug"]})
        else:
            return reverse("scripts", kwargs={"slug": self.kwargs["slug"]})

    @transaction.atomic
    def form_valid(self, form, *args, **kwargs):
        script = self.get_object()

        site = script.site
        site_membership = self.request.user.user_profile.sitemembership_set.filter(
            site_id=site.id
        ).first()
        if (
            not self.request.user.is_superuser
            and site_membership.site_user_type < site_membership.SITE_ADMIN
        ):
            raise PermissionDenied

        # Fetch the PCGroups for which it's an AssociatedScript before
        # we delete it from them
        # We create a list as the next command would change it
        scripts_pcgroups = list(PCGroup.objects.filter(policy__script=script))

        response = super(ScriptDelete, self).delete(form, *args, **kwargs)

        # For each of those groups update the script positions to avoid gaps
        for spcg in scripts_pcgroups:
            spcg.update_associated_script_positions()

        return response


class PCsView(SelectionMixin, SiteView):
    """If a site ha no computers it shows a page indicating that.
    If the site has at least one computer it redirects to that."""

    template_name = "system/pcs/site_pcs.html"
    selection_class = PC

    def get_list(self):
        return self.object.pcs.all()

    def render_to_response(self, context):
        if "selected_pc" in context:
            return HttpResponseRedirect(
                reverse(
                    "computer",
                    kwargs={
                        "slug": context["site"].uid,
                        "pc_uid": context["selected_pc"].uid,
                    },
                )
            )
        else:
            return super(PCsView, self).render_to_response(context)


class PCUpdate(SiteMixin, UpdateView, SuperAdminOrThisSiteMixin):
    template_name = "system/pcs/form.html"
    form_class = PCForm
    slug_field = "uid"

    VALID_ORDER_BY = []
    for i in [
        "pk",
        "batch__script__name",
        "started",
        "finished",
        "status",
        "batch__name",
    ]:
        VALID_ORDER_BY.append(i)
        VALID_ORDER_BY.append("-" + i)

    def get_object(self, queryset=None):
        try:
            site_id = get_object_or_404(Site, uid=self.kwargs["slug"])
            return PC.objects.get(uid=self.kwargs["pc_uid"], site=site_id)
        except PC.DoesNotExist:
            raise Http404(
                _("You have no computer with the following ID: %s")
                % self.kwargs["pc_uid"]
            )

    def get_context_data(self, **kwargs):
        context = super(PCUpdate, self).get_context_data(**kwargs)

        site = context["site"]
        form = context["form"]
        pc = self.object
        params = self.request.GET or self.request.POST

        context["pc_list"] = site.pcs.all()

        # Group picklist related:
        group_set = site.groups.all()
        selected_group_ids = form["pc_groups"].value()
        # template picklist requires the form pk, name, url (u)id.
        context["available_groups"] = group_set.exclude(
            pk__in=selected_group_ids
        ).values_list("pk", "name", "pk")
        context["selected_groups"] = group_set.filter(
            pk__in=selected_group_ids
        ).values_list("pk", "name", "pk")

        orderby = params.get("orderby", "-pk")
        if orderby not in JobSearch.VALID_ORDER_BY:
            orderby = "-pk"
        context["joblist"] = pc.jobs.order_by("status", "pk").order_by(orderby, "pk")

        if orderby.startswith("-"):
            context["orderby_key"] = orderby[1:]
            context["orderby_direction"] = "desc"
        else:
            context["orderby_key"] = orderby
            context["orderby_direction"] = "asc"

        context["orderby_base_url"] = pc.get_absolute_url() + "?"

        context["selected_pc"] = pc

        context["security_event"] = pc.security_events.latest_event()
        context["has_security_events"] = (
            pc.security_events.exclude(status=SecurityEvent.RESOLVED)
            .exclude(problem__level=EventLevels.NORMAL)
            .count()
            > 0
        )

        return context

    def form_valid(self, form):
        pc = self.object
        groups_pre = pc.pc_groups.all()

        selected_groups = form.cleaned_data["pc_groups"]
        verified_groups = selected_groups.intersection(groups_pre)
        unverified_groups = selected_groups.difference(groups_pre).order_by("name")

        previous_wake_plan = None
        for group in groups_pre:
            if group.wake_week_plan:
                previous_wake_plan = group.wake_week_plan
                break

        wake_plan = None
        for group in verified_groups:
            if group.wake_week_plan:
                wake_plan = group.wake_week_plan
                break

        run_wake_plan = False
        invalid_groups_names = []
        for group in unverified_groups:
            group_is_valid = True
            if wake_plan and group.wake_week_plan and wake_plan != group.wake_week_plan:
                invalid_groups_names.append(group.name)
                group_is_valid = False
            elif wake_plan is None and group.wake_week_plan:
                wake_plan = group.wake_week_plan
                if wake_plan != previous_wake_plan:
                    run_wake_plan = True
            if group_is_valid:
                verified_groups = verified_groups.union(
                    PCGroup.objects.filter(pk=group.pk)
                )

        form.cleaned_data["pc_groups"] = verified_groups

        if run_wake_plan and wake_plan.enabled:
            args_set = wake_plan.get_script_arguments()
            run_wake_plan_script(
                self.object.site,
                [self.object],
                args_set,
                self.request.user,
                type="set",
            )
        elif (
            (wake_plan is None or (wake_plan and not wake_plan.enabled))
            and previous_wake_plan
            and previous_wake_plan.enabled
        ):
            run_wake_plan_script(
                self.object.site,
                [self.object],
                [],
                self.request.user,
            )

        with transaction.atomic():
            pc.configuration.update_from_request(self.request.POST, "pc_config")
            response = super(PCUpdate, self).form_valid(form)

            # If this PC has joined any groups that have policies attached
            # to them, then run their scripts (first making sure that this
            # PC is capable of doing so!)
            groups_post = set(pc.pc_groups.all())
            new_groups = groups_post.difference(set(groups_pre))
            for g in new_groups:
                policy = g.ordered_policy
                if policy:
                    for asc in policy:
                        asc.run_on(self.request.user, [pc])
        if invalid_groups_names:
            invalid_groups_string = get_notification_string(invalid_groups_names)
            set_notification_cookie(
                response,
                _(
                    "Computer %s updated, but it could not be added to the group(s) %s "
                    "because it already belongs to the plan %s"
                )
                % (pc.name, invalid_groups_string, wake_plan.name),
                error=True,
            )
        else:
            set_notification_cookie(response, _("Computer %s updated") % pc.name)
        return response


class PCDelete(SiteMixin, SuperAdminOrThisSiteMixin, DeleteView):  # {{{
    model = PC
    template_name = "system/pcs/confirm_delete.html"

    def get_object(self, queryset=None):
        try:
            site_id = get_object_or_404(Site, uid=self.kwargs["slug"])
            return PC.objects.get(uid=self.kwargs["pc_uid"], site=site_id)
        except PC.DoesNotExist:
            raise Http404(
                _("You have no computer with the following ID: %s")
                % self.kwargs["pc_uid"]
            )

    def get_success_url(self):
        return reverse("computers", kwargs={"slug": self.kwargs["slug"]})


# TODO: Rename all of these to WakeWeekPlan* now they no longer handle WakeChangeEvents.
class WakePlanRedirect(RedirectView):
    def get_redirect_url(self, **kwargs):
        site = get_object_or_404(Site, uid=kwargs["slug"])

        wake_week_plans = WakeWeekPlan.objects.filter(site=site)

        if wake_week_plans.exists():
            wake_week_plan = wake_week_plans.first()
            return wake_week_plan.get_absolute_url()
        else:
            return reverse("wake_plan_new", args=[site.uid])


class WakePlanBaseMixin(SiteMixin, SuperAdminOrThisSiteMixin):
    # What's in common between both Create, Update and Delete
    def get_context_data(self, **kwargs):
        context = super(WakePlanBaseMixin, self).get_context_data(**kwargs)

        context["site"] = get_object_or_404(Site, uid=self.kwargs["slug"])
        plan = self.object
        context["selected_plan"] = plan
        context["wake_week_plans_list"] = WakeWeekPlan.objects.filter(
            site=context["site"]
        )

        context["wake_plan_access"] = (
            True
            if context["site"].customer.feature_permission.filter(uid="wake_plan")
            else False
        )

        return context


class WakePlanExtendedMixin(WakePlanBaseMixin):
    # What's in common between both Create and Update - but not Delete
    def get_context_data(self, **kwargs):
        context = super(WakePlanExtendedMixin, self).get_context_data(**kwargs)

        # These are shared between BaseMixin and ExtendedMixin - ideally they could just be inherited here
        context["site"] = get_object_or_404(Site, uid=self.kwargs["slug"])
        plan = self.object
        context["selected_plan"] = plan
        context["wake_week_plans_list"] = WakeWeekPlan.objects.filter(
            site=context["site"]
        )

        # Get the link to the user guide for the chosen language
        context["wake_plan_user_guide"] = (
            "https://github.com/OS2borgerPC/admin-site/raw/development/admin_site"
            + "/static/docs/Wake_plan_user_guide"
            + "_"
            + self.request.user.user_profile.language
            + ".pdf"
        )

        form = context["form"]
        # params = self.request.GET or self.request.POST

        # WakeChangeEvent picklist related:
        all_wake_change_events_set = context["site"].wake_change_events.all()
        selected_wake_change_event_ids = form["wake_change_events"].value()
        if not selected_wake_change_event_ids:
            selected_wake_change_event_ids = []
        # Fetching the entire object for this picklist so we can change the name for the event to include date/time info
        # template picklist requires the form pk, name, url (u)id.
        available_wake_change_events = all_wake_change_events_set.exclude(
            pk__in=selected_wake_change_event_ids
        ).order_by("-date_start", "name")
        context["available_wake_change_events"] = [
            (a.pk, a, a.pk) for a in available_wake_change_events
        ]
        selected_wake_change_events = all_wake_change_events_set.filter(
            pk__in=selected_wake_change_event_ids
        ).order_by("-date_start", "name")
        context["selected_wake_change_events"] = [
            (s.pk, s, s.pk) for s in selected_wake_change_events
        ]

        # Group picklist related:
        all_groups_set = context["site"].groups.all()
        selected_group_ids = form["groups"].value()
        # selected_group_ids = [group.id for group in plan.groups.all()]
        if not selected_group_ids:
            selected_group_ids = []
        # template picklist requires the form pk, name, url (u)id.
        context["available_groups"] = all_groups_set.exclude(
            pk__in=selected_group_ids
        ).values_list("pk", "name", "pk")
        context["selected_groups"] = all_groups_set.filter(
            pk__in=selected_group_ids
        ).values_list("pk", "name", "pk")

        return context

    def verify_and_add_groups_and_exceptions(self, form):
        # Adding wake change events
        # The string currently set to "wake_change_events" must match the submit name
        # chosen for the pick list used to add wake change events
        exceptions_pk = form["wake_change_events"].value()
        exceptions_selected = WakeChangeEvent.objects.filter(pk__in=exceptions_pk)
        # Get the related wake change events before the update
        exceptions_pre = self.object.wake_change_events.all()
        # Verify the pre-existing events that are still selected
        verified_exceptions = exceptions_pre.intersection(exceptions_selected)
        # Newly selected events must be verified
        unverified_exceptions = exceptions_selected.difference(exceptions_pre).order_by(
            "-date_start", "name"
        )
        invalid_exceptions_names = []
        # Using the same ordering as the list of events,
        # check if each newly selected event overlaps with any verified event.
        # If there is no overlap, verify the checked event. Each subsequently checked event
        # will thus also be checked for overlap with previously verified events.
        # Also get the names of the events that could not be verified.
        for exception in unverified_exceptions:
            exception_is_valid = True
            for valid_exception in verified_exceptions:
                if (
                    valid_exception.date_start
                    <= exception.date_start
                    <= valid_exception.date_end
                    or valid_exception.date_start
                    <= exception.date_end
                    <= valid_exception.date_end
                    or exception.date_start
                    <= valid_exception.date_start
                    <= exception.date_end
                ):
                    exception_is_valid = False
                    invalid_exceptions_names.append(exception.name)
                    break
            if exception_is_valid:
                verified_exceptions = verified_exceptions.union(
                    WakeChangeEvent.objects.filter(pk=exception.pk)
                )
        # Add the verified events to the plan
        self.object.wake_change_events.set(verified_exceptions)
        # Adding groups
        # The string currently set to "groups" must match the submit name
        # chosen for the pick list used to add groups
        groups_pk = form["groups"].value()
        groups = PCGroup.objects.filter(pk__in=groups_pk)
        # groups_with_other_plans_names = []
        # groups_without_other_plans_pk = []
        # for group in groups:
        #     if group.wake_week_plan and group.wake_week_plan != self.object:
        #         groups_with_other_plans_names.append(group.name)
        #     else:
        #         groups_without_other_plans_pk.append(group.pk)
        # groups = PCGroup.objects.filter(pk__in=groups_without_other_plans_pk)
        # Find the pcs in the groups
        pcs_in_groups_pk = list(set(groups.values_list("pcs", flat=True)))
        pcs_in_groups = PC.objects.filter(pk__in=pcs_in_groups_pk)
        # Find the pcs in the groups that belong to different wake plans
        pcs_with_other_plans = []
        pcs_with_other_plans_names = []
        other_plans_names = []
        for pc in pcs_in_groups:
            other_group_relations = pc.pc_groups.exclude(pk__in=groups_pk)
            for group in other_group_relations:
                if group.wake_week_plan and group.wake_week_plan != self.object:
                    pcs_with_other_plans.append(pc.pk)
                    pcs_with_other_plans_names.append(pc.name)
                    other_plans_names.append(group.wake_week_plan.name)
                    break
        pcs_with_other_plans = PC.objects.filter(pk__in=pcs_with_other_plans)
        # Verify the groups that do not include pcs belonging to a different wake plan
        # and get the names of the groups that could not be verified
        verified_groups_pk = []
        invalid_groups_names = []
        for group in groups:
            if not pcs_with_other_plans.intersection(group.pcs.all()):
                verified_groups_pk.append(group.pk)
            else:
                invalid_groups_names.append(group.name)
        verified_groups = PCGroup.objects.filter(pk__in=verified_groups_pk)
        # Add the verified groups to the plan
        for g in verified_groups:
            g.wake_week_plan = self.object
            g.save()
        # Get the pcs in the verified groups
        pcs_in_verified_groups_pk = list(
            set(verified_groups.values_list("pcs", flat=True))
        )
        pcs_in_verified_groups = PC.objects.filter(pk__in=pcs_in_verified_groups_pk)
        # Generate the notification strings
        invalid_groups_string = get_notification_string(invalid_groups_names)
        pcs_with_other_plans_string = get_notification_string(
            pcs_with_other_plans_names
        )
        other_plans_string = get_notification_string(
            other_plans_names, conjunction="eller"
        )
        invalid_events_string = get_notification_string(invalid_exceptions_names)
        return (
            pcs_in_verified_groups,
            invalid_groups_string,
            pcs_with_other_plans_string,
            other_plans_string,
            invalid_events_string,
            set(groups),
        )


class WakePlanCreate(WakePlanExtendedMixin, CreateView):
    model = WakeWeekPlan
    form_class = WakePlanForm
    slug_field = "slug"
    template_name = "system/wake_plan/wake_plan.html"

    def form_valid(self, form):
        # The form does not allow setting the site yourself, so we insert that here
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        if not site.customer.feature_permission.filter(uid="wake_plan"):
            raise PermissionDenied
        self.object = form.save(commit=False)
        self.object.site = site

        response = super(WakePlanCreate, self).form_valid(form)

        # Verify and add the selected groups
        # Also add the selected exceptions (no verification needed)
        (
            pcs_in_verified_groups,
            invalid_groups_string,
            pcs_with_other_plans_string,
            other_plans_string,
            invalid_events_string,
            groups_selected,
        ) = self.verify_and_add_groups_and_exceptions(form)

        # If pcs were added and the plan is enabled
        if pcs_in_verified_groups and self.object.enabled:
            args_set = self.object.get_script_arguments()
            run_wake_plan_script(
                self.object.site,
                pcs_in_verified_groups,
                args_set,
                self.request.user,
                type="set",
            )

        # If some groups or exceptions could not be verified, display this and the reason
        if invalid_groups_string and invalid_events_string:
            set_notification_cookie(
                response,
                _(
                    "PCWakePlan %s created, but the group(s) %s could not be added "
                    "because the pc(s) %s already belong to the plan(s) %s and "
                    "the WakeChangeEvents %s could not be added due to overlap"
                )
                % (
                    self.object.name,
                    invalid_groups_string,
                    pcs_with_other_plans_string,
                    other_plans_string,
                    invalid_events_string,
                ),
                error=True,
            )
        elif invalid_groups_string and not invalid_events_string:
            set_notification_cookie(
                response,
                _(
                    "PCWakePlan %s created, but the group(s) %s could not be added "
                    "because the pc(s) %s already belong to the plan(s) %s"
                )
                % (
                    self.object.name,
                    invalid_groups_string,
                    pcs_with_other_plans_string,
                    other_plans_string,
                ),
                error=True,
            )
        elif not invalid_groups_string and invalid_events_string:
            set_notification_cookie(
                response,
                _(
                    "PCWakePlan %s created, but the WakeChangeEvents %s could not be added "
                    "due to overlap"
                )
                % (self.object.name, invalid_events_string),
                error=True,
            )
        else:
            set_notification_cookie(
                response, _("PCWakePlan %s created") % self.object.name
            )

        return response


class WakePlanUpdate(WakePlanExtendedMixin, UpdateView):
    template_name = "system/wake_plan/wake_plan.html"
    form_class = WakePlanForm
    slug_field = "slug"

    def get_object(self, queryset=None):
        try:
            site_id = get_object_or_404(Site, uid=self.kwargs["slug"])
            return WakeWeekPlan.objects.get(
                id=self.kwargs["wake_week_plan_id"], site=site_id
            )
        except (WakeWeekPlan.DoesNotExist, ValueError):
            raise Http404(
                _("You have no Wake Week Plan with the following ID: %s")
                % self.kwargs["wake_week_plan_id"]
            )

    def form_valid(self, form):
        if not self.object.site.customer.feature_permission.filter(uid="wake_plan"):
            raise PermissionDenied
        # Ensure that if a start time has been set, so has the end time - or vice versa
        f = self.request.POST
        if (
            (f.get("monday_on") and not f.get("monday_off"))
            or (not f.get("monday_on") and f.get("monday_off"))
            or (f.get("tuesday_on") and not f.get("tuesday_off"))
            or (not f.get("tuesday_on") and f.get("tuesday_off"))
            or (f.get("wednesday_on") and not f.get("wednesday_off"))
            or (not f.get("wednesday_on") and f.get("wednesday_off"))
            or (f.get("thursday_on") and not f.get("thursday_off"))
            or (not f.get("thursday_on") and f.get("thursday_off"))
            or (f.get("friday_on") and not f.get("friday_off"))
            or (not f.get("friday_on") and f.get("friday_off"))
            or (f.get("saturday_on") and not f.get("saturday_off"))
            or (not f.get("saturday_on") and f.get("saturday_off"))
            or (f.get("sunday_on") and not f.get("sunday_off"))
            or (not f.get("sunday_on") and f.get("sunday_off"))
        ):
            return self.form_invalid(form)

        # Capture a view of the groups and settings before the update
        groups_pre = set(self.object.groups.all())
        plan_pre = self.get_object()
        enabled_pre = plan_pre.enabled
        events_pre = set(self.object.wake_change_events.all())

        with transaction.atomic():
            response = super(WakePlanUpdate, self).form_valid(form)

            (
                pcs_in_verified_groups,
                invalid_groups_string,
                pcs_with_other_plans_string,
                other_plans_string,
                invalid_events_string,
                groups_selected,
            ) = self.verify_and_add_groups_and_exceptions(form)

            # Remove the deselected groups from the wake plan and find the pc objects in those groups
            pcs_in_removed_groups = PC.objects.none()
            groups_removed = groups_pre.difference(groups_selected)
            for g in groups_removed:
                pcs_in_removed_groups = pcs_in_removed_groups.union(g.pcs.all())
                g.wake_week_plan = None
                g.save()

            # Get the status of the wake plan after the update
            enabled_post = self.object.enabled

            # Find all pc objects belonging to the wake plan after the update
            pcs_all = PC.objects.none()
            for g in self.object.groups.all():
                pcs_all = pcs_all.union(g.pcs.all())

            # If the wake plan was active before and after the update
            if enabled_pre and enabled_post:
                # Find the pc objects that belonged to the wake plan before the update
                pcs_pre = PC.objects.none()
                for g in groups_pre:
                    pcs_pre = pcs_pre.union(g.pcs.all())

                # Find the pcs that have been added or removed from the wake plan
                pcs_to_be_set = pcs_in_verified_groups.difference(pcs_pre)
                pcs_to_be_reset = pcs_in_removed_groups.difference(pcs_all)

                # Remove the wake plan from the pcs that have been removed
                if pcs_to_be_reset:
                    run_wake_plan_script(
                        self.object.site, pcs_to_be_reset, [], self.request.user
                    )

                # If the wake plan settings have changed, update the wake plan on all members
                if self.check_settings_updates(plan_pre, events_pre):
                    pcs_to_be_set = pcs_all

                # Set the wake plan on the pcs that need to have it updated
                if pcs_to_be_set:
                    # Get the arguments for setting the wake plan on a pc
                    args_set = self.object.get_script_arguments()
                    run_wake_plan_script(
                        self.object.site,
                        pcs_to_be_set,
                        args_set,
                        self.request.user,
                        type="set",
                    )

            # If the wake plan status was changed from active to inactive,
            # remove the wake plan from all members
            elif enabled_pre and not enabled_post:
                if pcs_all:
                    run_wake_plan_script(
                        self.object.site, pcs_all, [], self.request.user
                    )

            # If the wake plan status was changed from inactive to active,
            # set the wake plan on all members
            elif not enabled_pre and enabled_post:
                if pcs_all:
                    # Get the arguments for setting the wake plan on a pc
                    args_set = self.object.get_script_arguments()
                    run_wake_plan_script(
                        self.object.site,
                        pcs_all,
                        args_set,
                        self.request.user,
                        type="set",
                    )

            # If the wake plan status was inactive before and after the update
            else:
                pass

            # If some groups or exceptions could not be verified, display this and the reason
            if invalid_groups_string and invalid_events_string:
                set_notification_cookie(
                    response,
                    _(
                        "PCWakePlan %s updated, but the group(s) %s could not be added "
                        "because the pc(s) %s already belong to the plan(s) %s and "
                        "the WakeChangeEvents %s could not be added due to overlap"
                    )
                    % (
                        self.object.name,
                        invalid_groups_string,
                        pcs_with_other_plans_string,
                        other_plans_string,
                        invalid_events_string,
                    ),
                    error=True,
                )
            elif invalid_groups_string and not invalid_events_string:
                set_notification_cookie(
                    response,
                    _(
                        "PCWakePlan %s updated, but the group(s) %s could not be added "
                        "because the pc(s) %s already belong to the plan(s) %s"
                    )
                    % (
                        self.object.name,
                        invalid_groups_string,
                        pcs_with_other_plans_string,
                        other_plans_string,
                    ),
                    error=True,
                )
            elif not invalid_groups_string and invalid_events_string:
                set_notification_cookie(
                    response,
                    _(
                        "PCWakePlan %s updated, but the WakeChangeEvents %s could not be added "
                        "due to overlap"
                    )
                    % (self.object.name, invalid_events_string),
                    error=True,
                )
            else:
                set_notification_cookie(
                    response,
                    _("PCWakePlan %s updated") % self.object.name,
                )

            return response

    def form_invalid(self, form):
        return super(WakePlanUpdate, self).form_invalid(form)

    def check_settings_updates(self, plan_pre, events_pre):
        """Helper function used to check if the plan settings have changed."""
        plan_post = self.object
        if (
            plan_pre.sleep_state != plan_post.sleep_state
            or plan_pre.monday_open != plan_post.monday_open
            or (plan_post.monday_open and plan_pre.monday_on != plan_post.monday_on)
            or (plan_post.monday_open and plan_pre.monday_off != plan_post.monday_off)
            or plan_pre.tuesday_open != plan_post.tuesday_open
            or (plan_post.tuesday_open and plan_pre.tuesday_on != plan_post.tuesday_on)
            or (
                plan_post.tuesday_open and plan_pre.tuesday_off != plan_post.tuesday_off
            )
            or plan_pre.wednesday_open != plan_post.wednesday_open
            or (
                plan_post.wednesday_open
                and plan_pre.wednesday_on != plan_post.wednesday_on
            )
            or (
                plan_post.wednesday_open
                and plan_pre.wednesday_off != plan_post.wednesday_off
            )
            or plan_pre.thursday_open != plan_post.thursday_open
            or (
                plan_post.thursday_open
                and plan_pre.thursday_on != plan_post.thursday_on
            )
            or (
                plan_post.thursday_open
                and plan_pre.thursday_off != plan_post.thursday_off
            )
            or plan_pre.friday_open != plan_post.friday_open
            or (plan_post.friday_open and plan_pre.friday_on != plan_post.friday_on)
            or (plan_post.friday_open and plan_pre.friday_off != plan_post.friday_off)
            or plan_pre.saturday_open != plan_post.saturday_open
            or (
                plan_post.saturday_open
                and plan_pre.saturday_on != plan_post.saturday_on
            )
            or (
                plan_post.saturday_open
                and plan_pre.saturday_off != plan_post.saturday_off
            )
            or plan_pre.sunday_open != plan_post.sunday_open
            or (plan_post.sunday_open and plan_pre.sunday_on != plan_post.sunday_on)
            or (plan_post.sunday_open and plan_pre.sunday_off != plan_post.sunday_off)
            or events_pre != set(plan_post.wake_change_events.all())
        ):
            return True
        else:
            return False


class WakePlanDelete(WakePlanBaseMixin, DeleteView):
    model = WakeWeekPlan
    # slug_field = "slug"
    template_name = "system/wake_plan/confirm_delete.html"

    def get_object(self, queryset=None):
        try:
            site_id = get_object_or_404(Site, uid=self.kwargs["slug"])
            plan = WakeWeekPlan.objects.get(
                id=self.kwargs["wake_week_plan_id"], site=site_id
            )
        except (WakeWeekPlan.DoesNotExist, ValueError):
            raise Http404(
                _("You have no Wake Week Plan with the following ID: %s")
                % self.kwargs["wake_week_plan_id"]
            )
        if not plan.site.customer.feature_permission.filter(uid="wake_plan"):
            raise PermissionDenied
        return plan

    def get_success_url(self):
        # I wonder if one could just call the WakeWeekPlanRedirectView directly?
        return reverse("wake_plans", args=[self.kwargs["slug"]])

    def form_valid(self, form, *args, **kwargs):
        deleted_plan_name = WakeWeekPlan.objects.get(
            id=self.kwargs["wake_week_plan_id"]
        ).name

        # Remove the wake plan from all pcs that belonged to it
        plan = self.get_object()
        groups = plan.groups.all()
        if plan.enabled and groups:
            pcs_in_groups = PC.objects.none()
            for g in groups:
                pcs_in_groups = pcs_in_groups.union(g.pcs.all())
            run_wake_plan_script(plan.site, pcs_in_groups, [], self.request.user)

        response = super(WakePlanDelete, self).delete(form, *args, **kwargs)

        set_notification_cookie(
            response,
            _("Wake Week Plan %s deleted") % deleted_plan_name,
        )
        return response


class WakePlanDuplicate(RedirectView, SiteMixin, SuperAdminOrThisSiteMixin):
    model = WakeWeekPlan

    def get_redirect_url(self, **kwargs):
        object_to_copy = WakeWeekPlan.objects.get(id=kwargs["wake_week_plan_id"])
        if not object_to_copy.site.customer.feature_permission.filter(uid="wake_plan"):
            raise PermissionDenied

        # Before we remove the pk we duplicate all the associated events, as they're precisely related through the pk
        # TODO: For now we actually duplicate the events rather than refer to the same ones
        # Which we'd like to change in the future, so WakeWeekPlans can generally share events
        # ...and not only through copying
        events = []
        for event in object_to_copy.wake_change_events.all():
            event.id = None
            event.save()
            events.append(event)

        object_to_copy.pk = (
            None  # Remove its current pk so it gets a new one when saving
        )
        object_to_copy.name = f"Kopi af {object_to_copy.name}"
        # Now save the copied object to get a new ID, which is also required to bind the duplicated events to it
        object_to_copy.save()

        object_to_copy.wake_change_events.set(events)

        new_id = object_to_copy.pk
        return reverse(
            "wake_plan",
            kwargs={"slug": kwargs["slug"], "wake_week_plan_id": new_id},
        )


class WakeChangeEventBaseMixin(SiteMixin, SuperAdminOrThisSiteMixin):
    def get_context_data(self, **kwargs):
        context = super(WakeChangeEventBaseMixin, self).get_context_data(**kwargs)

        # Basically in common between both Create, Update and Delete, so consider refactoring out to a Mixin
        context["site"] = get_object_or_404(Site, uid=self.kwargs["slug"])
        event = self.object
        context["selected_event"] = event
        # Note: The sorting here needs to be the same in WakeChangeEventRedirect
        context["wake_change_events_list"] = WakeChangeEvent.objects.filter(
            site=context["site"]
        ).order_by("-date_start", "name", "pk")

        if event is not None and event.id:
            context["wake_plan_list_for_event"] = event.wake_week_plans.all()

        context["wake_plan_access"] = (
            True
            if context["site"].customer.feature_permission.filter(uid="wake_plan")
            else False
        )

        return context

    def validate_dates(self):
        event = self.object
        valid = True
        overlapping_event = ""
        plan_with_overlap = ""
        if event.date_end < event.date_start:
            valid = False
        if valid and event.id and event.wake_week_plans.all():
            for plan in event.wake_week_plans.all():
                other_events = plan.wake_change_events.exclude(pk=event.pk)
                for other_event in other_events:
                    if (
                        other_event.date_start
                        <= event.date_start
                        <= other_event.date_end
                        or other_event.date_start
                        <= event.date_end
                        <= other_event.date_end
                        or event.date_start <= other_event.date_start <= event.date_end
                    ):
                        valid = False
                        overlapping_event = other_event.name
                        plan_with_overlap = plan.name
                        break
                if not valid:
                    break
        return valid, overlapping_event, plan_with_overlap


class WakeChangeEventRedirect(RedirectView):
    def get_redirect_url(self, **kwargs):
        site = get_object_or_404(Site, uid=kwargs["slug"])

        # Note: The sorting here needs to be the same in WakeChangeEventBaseMixin
        wake_change_events = WakeChangeEvent.objects.filter(site=site).order_by(
            "-date_start", "name", "pk"
        )

        if wake_change_events.exists():
            wake_change_event = wake_change_events.first()
            return wake_change_event.get_absolute_url()
        else:
            return reverse("wake_change_event_new_altered_hours", args=[site.uid])


class WakeChangeEventUpdate(WakeChangeEventBaseMixin, UpdateView):
    template_name = "system/wake_plan/wake_change_events/wake_change_event.html"
    form_class = WakeChangeEventForm
    slug_field = "slug"

    def get_object(self, queryset=None):
        try:
            site_id = get_object_or_404(Site, uid=self.kwargs["slug"])
            return WakeChangeEvent.objects.get(
                id=self.kwargs["wake_change_event_id"], site=site_id
            )
        except (WakeChangeEvent.DoesNotExist, ValueError):
            raise Http404(
                _("You have no Wake Change Event with the following ID: %s")
                % self.kwargs["wake_change_event_id"]
            )

    def form_valid(self, form):
        if not self.object.site.customer.feature_permission.filter(uid="wake_plan"):
            raise PermissionDenied
        # Capture a view of the event before the update
        event_pre = self.get_object()

        valid, overlapping_event, plan_with_overlap = self.validate_dates()
        if valid:
            response = super(WakeChangeEventUpdate, self).form_valid(form)

            # If the settings have changed and the wake change event is used
            # by active wake plans, update the pcs connected to those plans
            if self.check_settings_updates(event_pre):
                for plan in self.object.wake_week_plans.all():
                    if plan.enabled:
                        pcs_to_be_set_pk = list(
                            set(plan.groups.all().values_list("pcs", flat=True))
                        )
                        if pcs_to_be_set_pk:
                            pcs_to_be_set = PC.objects.filter(pk__in=pcs_to_be_set_pk)
                            args_set = plan.get_script_arguments()

                            run_wake_plan_script(
                                self.object.site,
                                pcs_to_be_set,
                                args_set,
                                self.request.user,
                                type="set",
                            )

            set_notification_cookie(
                response,
                _("Wake Change Event %s updated") % self.object.name,
            )
        else:
            response = self.form_invalid(form)
            if overlapping_event:
                set_notification_cookie(
                    response,
                    _("The chosen dates would cause overlap with event %s in plan %s")
                    % (overlapping_event, plan_with_overlap),
                    error=True,
                )
            else:
                set_notification_cookie(
                    response,
                    _("The end date cannot be before the start date %s") % "",
                    error=True,
                )

        return response

    def form_invalid(self, form):
        return super(WakeChangeEventUpdate, self).form_invalid(form)

    def check_settings_updates(self, event_pre):
        """Helper function used to check if the settings have changed
        and the event is used by an active wake plan"""
        event_post = self.object
        wake_plans = event_post.wake_week_plans.all()
        active_plans = False
        for plan in wake_plans:
            if plan.enabled:
                active_plans = True
                break
        if not active_plans:
            return False
        if (
            event_pre.date_start != event_post.date_start
            or event_pre.date_end != event_post.date_end
            or event_pre.time_start != event_post.time_start
            or event_pre.time_end != event_post.time_end
        ):
            return True
        else:
            return False


class WakeChangeEventCreate(WakeChangeEventBaseMixin, CreateView):
    model = WakeChangeEvent
    form_class = WakeChangeEventForm
    slug_field = "slug"
    template_name = "system/wake_plan/wake_change_events/wake_change_event.html"

    def form_valid(self, form):
        # The form does not allow setting the site yourself, so we insert that here
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        if not site.customer.feature_permission.filter(uid="wake_plan"):
            raise PermissionDenied
        self.object = form.save(commit=False)
        self.object.site = site

        valid, overlapping_event, plan_with_overlap = self.validate_dates()
        if valid:
            response = super(WakeChangeEventCreate, self).form_valid(form)
        else:
            response = self.form_invalid(form)
            set_notification_cookie(
                response,
                _("The end date cannot be before the start date %s") % "",
                error=True,
            )

        return response

    def form_invalid(self, form):
        return super(WakeChangeEventCreate, self).form_invalid(form)


class WakeChangeEventDelete(WakeChangeEventBaseMixin, DeleteView):
    model = WakeChangeEvent
    slug_field = "slug"
    template_name = "system/wake_plan/wake_change_events/confirm_delete.html"

    def get_object(self, queryset=None):
        event = WakeChangeEvent.objects.get(id=self.kwargs["wake_change_event_id"])
        if not event.site.customer.feature_permission.filter(uid="wake_plan"):
            raise PermissionDenied
        return event

    def get_success_url(self):
        return reverse("wake_change_events", args=[self.kwargs["slug"]])

    def form_valid(self, form, *args, **kwargs):
        # Update all pcs belonging to active plans that used this event
        event = self.get_object()
        plans = set(event.wake_week_plans.all())

        response = super(WakeChangeEventDelete, self).delete(form, *args, **kwargs)

        for plan in plans:
            pcs_in_groups = PC.objects.none()
            groups = plan.groups.all()
            if plan.enabled and groups:
                for g in groups:
                    pcs_in_groups = pcs_in_groups.union(g.pcs.all())
                if pcs_in_groups:
                    args_set = plan.get_script_arguments()
                    run_wake_plan_script(
                        plan.site,
                        pcs_in_groups,
                        args_set,
                        self.request.user,
                        type="set",
                    )

        return response


class UserRedirect(RedirectView, SuperAdminOrThisSiteMixin):
    """Redirects to either an existing user if one exists, or to the create user page"""

    def get_redirect_url(self, **kwargs):
        site = get_object_or_404(Site, uid=kwargs["slug"])
        users_on_site = site.users
        if users_on_site.exists():
            if self.request.user in users_on_site:
                destination_user = self.request.user.username
            else:  # for superusers just go to the first user in the list
                destination_user = users_on_site.first().username

            return reverse(
                "user", kwargs={"slug": site.uid, "username": destination_user}
            )

        else:
            return reverse("new_user", args=[site.uid])


class UsersMixin(object):
    def add_site_to_context(self, context):
        self.site = get_object_or_404(Site, uid=self.kwargs["slug"])
        context["site"] = self.site
        return context

    def add_userlist_to_context(self, context):
        if "site" not in context:
            self.add_site_to_context(context)
        if self.request.user.is_superuser:
            context["user_list"] = context["site"].users
        else:
            context["user_list"] = context["site"].users.filter(
                user_profile__is_hidden=False
            )
        if (
            not self.request.user.is_superuser
            and not self.request.user.user_profile.sitemembership_set.filter(
                site_user_type=SiteMembership.CUSTOMER_ADMIN
            )
        ):
            context["user_list"] = context["user_list"].exclude(
                user_profile__sitemembership__site_user_type=SiteMembership.CUSTOMER_ADMIN
            )
        # Add information about outstanding security events.
        no_of_sec_events = SecurityEvent.objects.priority_events_for_site(
            self.site
        ).count()
        context["sec_events"] = no_of_sec_events
        return context

    def add_membership_to_context(self, context):
        if "user_list" not in context:
            self.add_userlist_to_context(context)
        request_user = self.request.user
        user_profile = request_user.user_profile
        site_membership = user_profile.sitemembership_set.filter(
            site=context["site"]
        ).first()

        if site_membership:
            loginusertype = site_membership.site_user_type
        else:
            loginusertype = 0

        context["form"].setup_usertype_choices(loginusertype, request_user.is_superuser)

        context["site_membership"] = site_membership
        return context


class UserLink(FormView, UsersMixin, SuperAdminOrThisSiteMixin):
    form_class = UserLinkForm
    template_name = "system/users/link.html"

    def get(self, request, *args, **kwargs):
        """
        Overwrite the get method to ensure that non-customer
        admins can't directly access the UserLink URL.
        """
        site = get_object_or_404(Site, uid=self.kwargs["slug"])

        if (
            not self.request.user.is_superuser
            and self.request.user.user_profile.sitemembership_set.get(
                site=site
            ).site_user_type
            != SiteMembership.CUSTOMER_ADMIN
        ):
            raise PermissionDenied
        response = super().get(request, *args, **kwargs)

        return response

    def get_context_data(self, **kwargs):
        context = super(UserLink, self).get_context_data(**kwargs)
        self.add_membership_to_context(context)

        site = context["site"]
        form = context["form"]

        # user list related
        user_profiles_for_customer_pk = site.customer.sites.values_list(
            "user_profiles", flat=True
        )
        # Limit the possible selections to users for this customer that
        # do not already have access to this site
        users_for_customer_not_on_this_site = User.objects.filter(
            user_profile__pk__in=user_profiles_for_customer_pk
        ).exclude(user_profile__sites=site)
        form.fields["linked_users"].queryset = users_for_customer_not_on_this_site

        return context

    def form_valid(self, form):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        # Ensure that only customer admins can use this functionality
        if (
            not self.request.user.is_superuser
            and self.request.user.user_profile.sitemembership_set.get(
                site=site
            ).site_user_type
            != SiteMembership.CUSTOMER_ADMIN
        ):
            raise PermissionDenied
        selected_users = form.cleaned_data["linked_users"]
        selected_user_type = form.cleaned_data["usertype"]
        selected_users_names = []
        # Add the selected users to the site with
        # the selected user type
        for user in selected_users:
            selected_users_names.append(user.username)
            SiteMembership.objects.create(
                user_profile=user.user_profile,
                site=site,
                site_user_type=selected_user_type,
            )
        response = super(UserLink, self).form_valid(form)

        if selected_users_names:
            added_users_string = get_notification_string(selected_users_names)
            set_notification_cookie(
                response,
                _("The user(s) %s have been added to the site %s ")
                % (
                    added_users_string,
                    site.name,
                ),
            )

        return response

    def get_success_url(self):
        return reverse(
            "link_users",
            kwargs={
                "slug": self.kwargs["slug"],
            },
        )


class UserCreate(CreateView, UsersMixin, SuperAdminOrThisSiteMixin):
    model = User
    form_class = UserForm
    template_name = "system/users/update.html"

    def get_form_kwargs(self):
        kwargs = super(UserCreate, self).get_form_kwargs()
        kwargs["language"] = self.request.user.user_profile.language
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(UserCreate, self).get_context_data(**kwargs)
        self.add_membership_to_context(context)
        return context

    def form_valid(self, form):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        site_membership = self.request.user.user_profile.sitemembership_set.filter(
            site=site
        ).first()

        if self.request.user.is_superuser:
            site_membership = self.request.user.user_profile.sitemembership_set.first()

        if (
            self.request.user.is_superuser
            or site_membership.site_user_type >= site_membership.SITE_ADMIN
        ):
            self.object = form.save()
            user_profile = UserProfile.objects.create(user=self.object)
            # If a customer admin user is being created, ensure that
            # they have access to all sites for this customer
            if int(form.cleaned_data["usertype"]) == SiteMembership.CUSTOMER_ADMIN:
                for customer_site in site.customer.sites.all():
                    SiteMembership.objects.create(
                        user_profile=user_profile,
                        site=customer_site,
                        site_user_type=form.cleaned_data["usertype"],
                    )
            # If a non-customer admin user is being created,
            # only give them access to this site
            else:
                SiteMembership.objects.create(
                    user_profile=user_profile,
                    site=site,
                    site_user_type=form.cleaned_data["usertype"],
                )
            user_profile.language = form.cleaned_data["language"]
            user_profile.save()
            if int(form.cleaned_data["usertype"]) >= site_membership.SITE_ADMIN:
                self.object.user_permissions.set(
                    Permission.objects.filter(name="Can view login log")
                )
                self.object.is_staff = True
            result = super(UserCreate, self).form_valid(form)
            return result
        else:
            raise PermissionDenied

    def get_success_url(self):
        return reverse(
            "user",
            kwargs={
                "slug": self.kwargs["slug"],
                "username": self.object.username,
            },
        )


class UserUpdate(UpdateView, UsersMixin, SuperAdminOrThisSiteMixin):
    model = User
    form_class = UserForm
    template_name = "system/users/update.html"

    def get_object(self, queryset=None):
        try:
            self.selected_user = User.objects.get(username=self.kwargs["username"])
            site_membership = self.selected_user.user_profile.sitemembership_set.get(
                site__uid=self.kwargs["slug"]
            )
        except (User.DoesNotExist, SiteMembership.DoesNotExist):
            raise Http404(
                _("You have no user with the following ID: %s")
                % self.kwargs["username"]
            )
        if (
            site_membership.site_user_type == SiteMembership.CUSTOMER_ADMIN
            and not self.request.user.is_superuser
            and not self.request.user.user_profile.sitemembership_set.filter(
                site_user_type=SiteMembership.CUSTOMER_ADMIN
            )
        ):
            raise PermissionDenied

        return self.selected_user

    def get_context_data(self, **kwargs):
        # This line is necessary, as without it UserUpdate will think that user = selected_user
        self.context_object_name = "selected_user"
        context = super(UserUpdate, self).get_context_data(**kwargs)
        self.add_membership_to_context(context)

        context["selected_user"] = User.objects.get(username=self.kwargs["username"])

        if context["selected_user"].user_profile.sitemembership_set.filter(
            site_user_type=SiteMembership.CUSTOMER_ADMIN
        ):
            context["not_customer_admin"] = False
        else:
            context["not_customer_admin"] = True

        return context

    def get_form_kwargs(self):
        kwargs = super(UserUpdate, self).get_form_kwargs()
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        kwargs["site"] = site

        return kwargs

    def form_valid(self, form):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        site_membership_req_user = (
            self.request.user.user_profile.sitemembership_set.filter(site=site).first()
        )
        if (
            self.request.user.is_superuser
            or site_membership_req_user.site_user_type
            >= site_membership_req_user.SITE_ADMIN
            or self.request.user == self.selected_user
        ):
            self.object = form.save()

            user_profile = self.object.user_profile
            site_membership = user_profile.sitemembership_set.get(
                site=site, user_profile=user_profile
            )
            # If a user was made a customer admin, ensure that they have access
            # to all sites for this customer
            if (
                site_membership.site_user_type != int(form.cleaned_data["usertype"])
                and int(form.cleaned_data["usertype"]) == SiteMembership.CUSTOMER_ADMIN
            ):
                for customer_site in site.customer.sites.all():
                    try:
                        customer_site_membership = user_profile.sitemembership_set.get(
                            site=customer_site
                        )
                        customer_site_membership.site_user_type = form.cleaned_data[
                            "usertype"
                        ]
                        customer_site_membership.save()
                    except SiteMembership.DoesNotExist:
                        SiteMembership.objects.create(
                            user_profile=user_profile,
                            site=customer_site,
                            site_user_type=form.cleaned_data["usertype"],
                        )
            # If a customer admin was changed to a less privileged user type,
            # update all their site memberships to reflect this
            elif (
                site_membership.site_user_type != int(form.cleaned_data["usertype"])
                and site_membership.site_user_type == SiteMembership.CUSTOMER_ADMIN
            ):
                for customer_site_membership in user_profile.sitemembership_set.filter(
                    site__customer=site.customer
                ):
                    customer_site_membership.site_user_type = form.cleaned_data[
                        "usertype"
                    ]
                    customer_site_membership.save()
            else:
                site_membership.site_user_type = form.cleaned_data["usertype"]
                site_membership.save()
            if (
                not self.selected_user.is_superuser
                and int(form.cleaned_data["usertype"]) >= site_membership.SITE_ADMIN
            ):
                self.object.user_permissions.set(
                    Permission.objects.filter(name="Can view login log")
                )
                self.object.is_staff = True
            elif (
                not self.selected_user.is_superuser
                and int(form.cleaned_data["usertype"]) < site_membership.SITE_ADMIN
            ):
                self.object.is_staff = False
            user_profile.language = form.cleaned_data["language"]
            user_profile.save()
            response = super(UserUpdate, self).form_valid(form)
            set_notification_cookie(
                response, _("User %s updated") % self.object.username
            )
            return response
        else:
            raise PermissionDenied

    def get_success_url(self):
        return reverse(
            "user",
            kwargs={
                "slug": self.kwargs["slug"],
                "username": self.object.username,
            },
        )


class UserDelete(DeleteView, UsersMixin, SuperAdminOrThisSiteMixin):
    model = User
    template_name = "system/users/confirm_delete.html"

    def get_object(self, queryset=None):
        try:
            self.selected_user = User.objects.get(username=self.kwargs["username"])
            site_membership = self.selected_user.user_profile.sitemembership_set.get(
                site__uid=self.kwargs["slug"]
            )
        except (User.DoesNotExist, SiteMembership.DoesNotExist):
            raise Http404(
                _("You have no user with the following ID: %s")
                % self.kwargs["username"]
            )
        if (
            site_membership.site_user_type == SiteMembership.CUSTOMER_ADMIN
            and not self.request.user.is_superuser
        ):
            raise PermissionDenied
        return self.selected_user

    def get_context_data(self, **kwargs):
        context = super(UserDelete, self).get_context_data(**kwargs)
        self.add_userlist_to_context(context)
        context["selected_user"] = self.selected_user

        return context

    def get_success_url(self):
        return reverse("users", kwargs={"slug": self.kwargs["slug"]})

    def form_valid(self, form, *args, **kwargs):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        site_membership = self.request.user.user_profile.sitemembership_set.filter(
            site_id=site.id
        ).first()
        if (
            not self.request.user.is_superuser
            and site_membership.site_user_type < site_membership.SITE_ADMIN
        ):
            raise PermissionDenied
        # If the selected_user is a member of multiple sites, only remove them from this site
        if len(self.object.user_profile.sitemembership_set.all()) > 1:
            self.object.user_profile.sitemembership_set.get(site_id=site.id).delete()
            response = HttpResponseRedirect(self.get_success_url())
            set_notification_cookie(
                response,
                _("User %s removed from the site %s")
                % (self.kwargs["username"], site.name),
            )
        else:
            response = super(UserDelete, self).delete(form, *args, **kwargs)
            set_notification_cookie(
                response, _("User %s deleted") % self.kwargs["username"]
            )
        return response


class ConfigurationEntryCreate(SiteMixin, CreateView, SuperAdminOrThisSiteMixin):
    model = ConfigurationEntry
    form_class = ConfigurationEntryForm

    def form_valid(self, form):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        self.object = form.save(commit=False)
        self.object.owner_configuration = site.configuration

        return super(ConfigurationEntryCreate, self).form_valid(form)

    def get_success_url(self):
        return reverse("settings", kwargs={"slug": self.kwargs["slug"]})


class ConfigurationEntryUpdate(SiteMixin, UpdateView, SuperAdminOrThisSiteMixin):
    model = ConfigurationEntry
    form_class = ConfigurationEntryForm

    def get_success_url(self):
        return reverse("settings", kwargs={"slug": self.kwargs["slug"]})


class PCGroupRedirect(RedirectView, SuperAdminOrThisSiteMixin):
    def get_redirect_url(self, **kwargs):
        site = get_object_or_404(Site, uid=kwargs["slug"])

        pc_groups = PCGroup.objects.filter(site=site)

        if pc_groups.exists():
            group = pc_groups.first()
            return group.get_absolute_url()
        else:
            return reverse("new_group", args=[site.uid])


class PCGroupCreate(SiteMixin, CreateView, SuperAdminOrThisSiteMixin):
    form_class = PCGroupForm
    model = PCGroup
    slug_field = "uid"
    template_name = "system/pcgroups/site_groups.html"

    def get_context_data(self, **kwargs):
        context = super(PCGroupCreate, self).get_context_data(**kwargs)

        context["newform"] = PCGroupForm()
        del context["newform"].fields["pcs"]
        del context["newform"].fields["supervisors"]
        return context

    def render_to_response(self, context):
        if context["site"].groups.all():
            return HttpResponseRedirect(
                reverse("groups", kwargs={"slug": self.kwargs["slug"]})
            )
        else:
            return super(PCGroupCreate, self).render_to_response(context)

    def form_valid(self, form):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        self.object = form.save(commit=False)
        self.object.site = site

        return super(PCGroupCreate, self).form_valid(form)


class PCGroupUpdate(SiteMixin, SuperAdminOrThisSiteMixin, UpdateView):
    template_name = "system/pcgroups/site_groups.html"
    form_class = PCGroupForm
    model = PCGroup

    def get_object(self, queryset=None):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        try:
            # Groups used to be identified by a string UID, so sometimes we get lookups for a string which caused a
            # server error. Hence this explicit attempt to convert it to an int first.
            id = int(self.kwargs["group_id"])
            return PCGroup.objects.get(id=id, site=site)
        except (PCGroup.DoesNotExist, ValueError):
            raise Http404(
                _(
                    "You have no group with the following ID: %s. Try locating the group in the list of groups."
                )
                % self.kwargs["group_id"]
            )

    def get_context_data(self, **kwargs):
        context = super(PCGroupUpdate, self).get_context_data(**kwargs)

        group = self.object
        form = context["form"]
        site = context["site"]

        # Manually create a list of security problems to not only include those attached but also those
        # unattached which currently apply to all groups
        event_rules_incl_site = list(
            group.securityproblem.all().union(
                SecurityProblem.objects.filter(alert_groups=None, site=site)
            )
        )
        event_rules_incl_site.extend(
            list(
                group.eventruleserver.all().union(
                    EventRuleServer.objects.filter(alert_groups=None, site=site)
                )
            )
        )
        context["event_rules_incl_site"] = event_rules_incl_site

        # PC picklist related
        pc_queryset = site.pcs.filter(is_activated=True)
        form.fields["pcs"].queryset = pc_queryset

        selected_pc_ids = form["pcs"].value()
        context["available_pcs"] = pc_queryset.exclude(
            pk__in=selected_pc_ids
        ).values_list("pk", "name", "uid")
        context["selected_pcs"] = pc_queryset.filter(
            pk__in=selected_pc_ids
        ).values_list("pk", "name", "uid")

        # supervisor picklist related
        user_set = User.objects.filter(user_profile__sites=site)
        selected_user_ids = form["supervisors"].value()
        context["available_users"] = user_set.exclude(
            pk__in=selected_user_ids
        ).values_list("pk", "username", "username")
        context["selected_users"] = user_set.filter(
            pk__in=selected_user_ids
        ).values_list("pk", "username", "username")

        context["selected_group"] = group

        context["newform"] = PCGroupForm()
        del context["newform"].fields["pcs"]
        del context["newform"].fields["supervisors"]

        context["all_scripts"] = Script.objects.filter(
            Q(site=site) | Q(site=None),
            Q(is_hidden=False)
            | Q(feature_permission__in=site.customer.feature_permission.all()),
            is_security_script=False,
        )

        return context

    def form_valid(self, form):
        # Capture a view of the group's PCs and policy scripts before the
        # update
        members_pre = set(self.object.pcs.all())
        policy_pre = set(self.object.policy.all())
        # If the group is a member of a wake_plan,
        # prevent people from adding pcs that belong to a different wake_plan
        pcs_with_other_plans_names = []
        if self.object.wake_week_plan:
            # Find the pcs being added
            selected_pcs = form.cleaned_data["pcs"]
            new_pcs = set(selected_pcs).difference(members_pre)
            # Find the pcs being added that belong to a different wake plan
            pcs_with_other_plans_pk = []
            other_plans_names = []
            for pc in new_pcs:
                other_groups = pc.pc_groups.exclude(pk=self.object.pk)
                for g in other_groups:
                    if (
                        g.wake_week_plan
                        and g.wake_week_plan != self.object.wake_week_plan
                    ):
                        pcs_with_other_plans_pk.append(pc.pk)
                        pcs_with_other_plans_names.append(pc.name)
                        other_plans_names.append(g.wake_week_plan.name)
                        break
            pcs_with_other_plans = PC.objects.filter(pk__in=pcs_with_other_plans_pk)
            # Only add the pcs that do not belong to a different wake plan
            form.cleaned_data["pcs"] = selected_pcs.difference(pcs_with_other_plans)

        try:
            with transaction.atomic():
                self.object.configuration.update_from_request(
                    self.request.POST, "group_configuration"
                )
                updated_policy_scripts = self.object.update_policy_from_request(
                    self.request, "group_policies"
                )

                response = super(PCGroupUpdate, self).form_valid(form)

                members_post = set(self.object.pcs.all())
                policy_post = set(self.object.policy.all())

                # Work out which PCs and policy scripts have come and gone
                surviving_members = members_post.intersection(members_pre)
                new_members = members_post.difference(members_pre)
                new_policy = policy_post.difference(policy_pre)
                removed_members = members_pre.difference(members_post)

                # Run all policy scripts on new PCs...
                if new_members:
                    ordered_policy = list(policy_post)
                    ordered_policy.sort(key=lambda asc: asc.position)
                    for asc in ordered_policy:
                        asc.run_on(self.request.user, new_members)

                if self.object.site.rerun_asc:
                    policy_for_all = new_policy.union(updated_policy_scripts)
                else:
                    policy_for_all = new_policy
                policy_for_all = list(policy_for_all)
                policy_for_all.sort(key=lambda asc: asc.position)
                # ... and run new policy scripts on old PCs
                for asc in policy_for_all:
                    asc.run_on(self.request.user, surviving_members)

                # If the group belongs to an active wake plan
                if self.object.wake_week_plan and self.object.wake_week_plan.enabled:
                    if new_members or removed_members:
                        # Find the other groups belonging to the same wake plan as this one
                        other_wake_plan_groups = (
                            self.object.wake_week_plan.groups.exclude(pk=self.object.pk)
                        )
                        # Find the pcs in the other groups belonging to the same wake plan as this group
                        pcs_in_other_wake_plan_groups = PC.objects.none()
                        for g in other_wake_plan_groups:
                            pcs_in_other_wake_plan_groups = (
                                pcs_in_other_wake_plan_groups.union(g.pcs.all())
                            )
                    # If the group has new members that do not already belong to the wake plan
                    # via a different group, set the wake plan on those members
                    if new_members:
                        new_wake_plan_members = []
                        for member in new_members:
                            if member not in pcs_in_other_wake_plan_groups:
                                new_wake_plan_members.append(member.pk)
                        if new_wake_plan_members:
                            args_set = self.object.wake_week_plan.get_script_arguments()
                            pcs_to_be_set = PC.objects.filter(
                                pk__in=new_wake_plan_members
                            )
                            run_wake_plan_script(
                                self.object.site,
                                pcs_to_be_set,
                                args_set,
                                self.request.user,
                                type="set",
                            )

                    # If pcs have been removed from the group that do not still belong to the wake plan
                    # via a different group, remove the wake plan from those pcs
                    if removed_members:
                        removed_wake_plan_members = []
                        for member in removed_members:
                            if member not in pcs_in_other_wake_plan_groups:
                                removed_wake_plan_members.append(member.pk)
                        if removed_wake_plan_members:
                            pcs_to_be_reset = PC.objects.filter(
                                pk__in=removed_wake_plan_members
                            )
                            run_wake_plan_script(
                                self.object.site, pcs_to_be_reset, [], self.request.user
                            )

                # If some pcs could not be added due to belonging to a different wake plan,
                # display this and the reason
                if pcs_with_other_plans_names:
                    (
                        pcs_with_other_plans_string,
                        other_plans_string,
                    ) = self.get_notification_strings(
                        pcs_with_other_plans_names, other_plans_names
                    )
                    set_notification_cookie(
                        response,
                        _(
                            "Group %s updated, but the pc(s) %s could not be added "
                            "because they already belong to the plan(s) %s"
                        )
                        % (
                            self.object.name,
                            pcs_with_other_plans_string,
                            other_plans_string,
                        ),
                        error=True,
                    )
                else:
                    set_notification_cookie(
                        response,
                        _("Group %s updated") % self.object.name,
                    )

                return response
        except MandatoryParameterMissingError as e:
            # If this happens, it happens *before* we have a valid
            # HttpResponse, so make one with form_invalid()
            response = self.form_invalid(form)
            parameter = e.args[0]
            set_notification_cookie(
                response,
                _("No value was specified for the mandatory input %s" " of script %s")
                % (parameter.name, parameter.script.name),
                error=True,
            )
            return response

    def form_invalid(self, form):
        return super(PCGroupUpdate, self).form_invalid(form)

    def get_notification_strings(self, pc_names, plan_names):
        """Helper function used to generate strings for the notification displayed
        when selected pcs could not be added."""
        pc_names = list(set(pc_names))
        plan_names = list(set(plan_names))
        if len(pc_names) > 1:
            pc_string = ", ".join(pc_names[:-1])
            pc_string = "".join([pc_string, " og ", pc_names[-1]])
        else:
            pc_string = pc_names[0]

        if len(plan_names) > 1:
            plan_string = ", ".join(plan_names[:-1])
            plan_string = "".join([plan_string, " eller ", plan_names[-1]])
        else:
            plan_string = plan_names[0]
        return pc_string, plan_string


class PCGroupDelete(SiteMixin, SuperAdminOrThisSiteMixin, DeleteView):
    template_name = "system/pcgroups/confirm_delete.html"
    model = PCGroup

    def get_object(self, queryset=None):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        try:
            # Groups used to be identified by a string UID, so sometimes we get lookups for a string which caused a
            # server error. Hence this explicit attempt to convert it to an int first.
            id = int(self.kwargs["group_id"])
            return PCGroup.objects.get(id=id, site=site)
        except (PCGroup.DoesNotExist, ValueError):
            raise Http404(
                _(
                    "You have no group with the following ID: %s. Try locating the group in the list of groups."
                )
                % self.kwargs["group_id"]
            )

    def get_success_url(self):
        return reverse("groups", kwargs={"slug": self.kwargs["slug"]})

    def form_valid(self, form, *args, **kwargs):
        self_object = self.get_object()
        name = self_object.name
        # wake_week_plan-related
        members = self_object.pcs.all()
        # If this group had an active wake plan and members
        if (
            self_object.wake_week_plan
            and self_object.wake_week_plan.enabled
            and members
        ):
            # Find the other groups belonging to the same wake plan as this group
            other_wake_plan_groups = self_object.wake_week_plan.groups.exclude(
                pk=self_object.pk
            )
            # Find the pcs in the other groups belonging to the same wake plan as this group
            pcs_in_other_wake_plan_groups = PC.objects.none()
            for g in other_wake_plan_groups:
                pcs_in_other_wake_plan_groups = pcs_in_other_wake_plan_groups.union(
                    g.pcs.all()
                )
            # If this group had members that do not still belong to the wake plan
            # via a different group, remove the wake plan from those members
            pcs_to_be_reset = members.difference(pcs_in_other_wake_plan_groups)
            if pcs_to_be_reset:
                run_wake_plan_script(
                    self_object.site, pcs_to_be_reset, [], self.request.user
                )

        response = super(PCGroupDelete, self).delete(form, *args, **kwargs)
        set_notification_cookie(response, _("Group %s deleted") % name)
        return response


class EventRuleRedirect(RedirectView, SuperAdminOrThisSiteMixin):
    def get_redirect_url(self, **kwargs):
        site = get_object_or_404(Site, uid=kwargs["slug"])

        # TODO: Make these related_names plural :/
        security_problem = site.securityproblem.all().order_by("name").first()
        event_rule_server = site.eventruleserver.all().order_by("name").first()

        if security_problem is not None or event_rule_server is not None:
            if security_problem and event_rule_server:
                alphabetically_first = sorted(
                    [security_problem, event_rule_server], key=lambda x: x.name
                )[0]
                return alphabetically_first.get_absolute_url()
            elif security_problem and event_rule_server is None:
                return security_problem.get_absolute_url()
            elif security_problem is None and event_rule_server:
                return event_rule_server.get_absolute_url()
        else:
            return reverse("event_rule_security_problem_new", args=[site.uid])


class EventRuleBaseMixin(SiteMixin, SuperAdminOrThisSiteMixin):
    def get_context_data(self, **kwargs):
        context = super(EventRuleBaseMixin, self).get_context_data(**kwargs)

        # For the menu: Gather all security problems and notifications rules and sort them
        event_listeners = list(context["site"].securityproblem.all())
        event_listeners.extend(list(context["site"].eventruleserver.all()))
        context["event_listeners"] = sorted(event_listeners, key=lambda x: x.name)

        site = context["site"]
        form = context["form"]
        group_set = site.groups.all()

        selected_group_ids = form["alert_groups"].value() or []

        # template picklist requires the form pk, name, url (u)id.
        context["available_groups"] = group_set.exclude(
            pk__in=selected_group_ids
        ).values_list("pk", "name", "pk")
        context["selected_groups"] = group_set.filter(
            pk__in=selected_group_ids
        ).values_list("pk", "name", "pk")

        user_set = User.objects.filter(user_profile__sites=site)
        selected_user_ids = form["alert_users"].value() or []

        context["available_users"] = user_set.exclude(
            pk__in=selected_user_ids
        ).values_list("pk", "username", "username")
        context["selected_users"] = user_set.filter(
            pk__in=selected_user_ids
        ).values_list("pk", "username", "username")

        # This first approach would be nicer, but SecurityProblemForm is only defined if it's a SecuirtyProblemForm
        # It's not a Form we've overridden currently, so it's also not importable from system.forms. But maybe there's another way to import it?
        # if type(form) is SecurityProblemForm:
        if form.__class__.__name__ == "SecurityProblemForm":
            # Limit list of scripts to only include security scripts.
            script_set = Script.objects.filter(
                Q(site__isnull=True) | Q(site=site),
                is_security_script=True,
            )
            form.fields["security_script"].queryset = script_set

        # Extra fields
        context["selected"] = self.object

        request_user = self.request.user
        context["site_membership"] = (
            request_user.user_profile.sitemembership_set.filter(site_id=site.id).first()
        )
        return context

    def form_valid(self, form):
        response = super(__class__, self).form_valid(form)

        notification_changes_saved(response, self.request.user.user_profile.language)

        return response


class SecurityProblemCreate(EventRuleBaseMixin, CreateView):
    template_name = "system/event_rules/site_security_problems.html"
    model = SecurityProblem
    fields = "__all__"


class SecurityProblemUpdate(EventRuleBaseMixin, UpdateView):
    template_name = "system/event_rules/site_security_problems.html"
    model = SecurityProblem
    fields = "__all__"

    def get_object(self, queryset=None):
        try:
            return SecurityProblem.objects.get(
                id=self.kwargs["id"], site__uid=self.kwargs["slug"]
            )
        except SecurityProblem.DoesNotExist:
            raise Http404(
                _("You have no Security Problem with the following ID: %s")
                % self.kwargs["id"]
            )


class SecurityProblemDelete(SiteMixin, DeleteView, SuperAdminOrThisSiteMixin):
    template_name = "system/event_rules/security_problem_confirm_delete.html"
    model = SecurityProblem

    def get_object(self, queryset=None):
        return SecurityProblem.objects.get(
            id=self.kwargs["id"], site__uid=self.kwargs["slug"]
        )

    def get_success_url(self):
        return reverse("event_rules", args=[self.kwargs["slug"]])

    def form_valid(self, form, *args, **kwargs):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        site_membership = self.request.user.user_profile.sitemembership_set.filter(
            site_id=site.id
        ).first()
        if (
            not self.request.user.is_superuser
            and site_membership.site_user_type < site_membership.SITE_ADMIN
        ):
            raise PermissionDenied
        response = super(SecurityProblemDelete, self).delete(form, *args, **kwargs)
        return response


class EventRuleServerCreate(EventRuleBaseMixin, CreateView):
    template_name = "system/event_rules/site_event_rules_server.html"
    model = EventRuleServer
    slug_field = "slug"
    form_class = EventRuleServerForm


class EventRuleServerUpdate(EventRuleBaseMixin, UpdateView):
    template_name = "system/event_rules/site_event_rules_server.html"
    model = EventRuleServer
    form_class = EventRuleServerForm

    def get_object(self, queryset=None):
        try:
            return EventRuleServer.objects.get(
                id=self.kwargs["id"], site__uid=self.kwargs["slug"]
            )
        except EventRuleServer.DoesNotExist:
            raise Http404(
                _("You have no Event Rule Server with the following ID: %s")
                % self.kwargs["id"]
            )


class EventRuleServerDelete(SiteMixin, DeleteView, SuperAdminOrThisSiteMixin):
    template_name = "system/event_rules/event_rule_server_confirm_delete.html"
    model = EventRuleServer

    def get_object(self, queryset=None):
        return EventRuleServer.objects.get(
            id=self.kwargs["id"], site__uid=self.kwargs["slug"]
        )

    def get_success_url(self):
        return reverse("event_rules", args=[self.kwargs["slug"]])

    def form_valid(self, form, *args, **kwargs):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        site_membership = self.request.user.user_profile.sitemembership_set.filter(
            site_id=site.id
        ).first()
        if (
            not self.request.user.is_superuser
            and site_membership.site_user_type < site_membership.SITE_ADMIN
        ):
            raise PermissionDenied
        response = super(EventRuleServerDelete, self).delete(form, *args, **kwargs)
        return response


class SecurityEventsView(SiteView):
    template_name = "system/security_events/site_security_events.html"

    def get_context_data(self, **kwargs):
        # First, get basic context from superclass
        context = super(SecurityEventsView, self).get_context_data(**kwargs)
        # Supply extra info as needed.
        level_preselected = set([EventLevels.CRITICAL, EventLevels.HIGH])
        context["level_choices"] = [
            {
                "name": name,
                "value": value,
                "label": EventLevels.LEVEL_TO_LABEL[value],
                "checked": 'checked="checked' if value in level_preselected else "",
            }
            for (value, name) in EventLevels.LEVEL_CHOICES
        ]
        status_preselected = set([SecurityEvent.NEW, SecurityEvent.ASSIGNED])
        context["status_choices"] = [
            {
                "name": name,
                "value": value,
                "label": SecurityEvent.STATUS_TO_LABEL[value],
                "checked": 'checked="checked' if value in status_preselected else "",
            }
            for (value, name) in SecurityEvent.STATUS_CHOICES
        ]

        context["form"] = SecurityEventForm()
        qs = context["form"].fields["assigned_user"].queryset
        qs = qs.filter(user_profile__sites=self.get_object())
        context["form"].fields["assigned_user"].queryset = qs

        return context


class SecurityEventSearch(SiteMixin, JSONResponseMixin, BaseListView):
    paginate_by = 20
    http_method_names = ["get"]
    VALID_ORDER_BY = []
    for i in ["pk", "problem__name", "occurred_time", "assigned_user__username"]:
        VALID_ORDER_BY.append(i)
        VALID_ORDER_BY.append("-" + i)

    def render_to_response(self, context, **response_kwargs):
        return self.render_to_json_response(context, **response_kwargs)

    def get_queryset(self):
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        queryset = SecurityEvent.objects.filter(
            Q(problem__site=site) | Q(event_rule_server__site=site)
        )
        params = self.request.GET

        if "level" in params:
            queryset = queryset.filter(
                Q(problem__level__in=params.getlist("level"))
                | Q(event_rule_server__level__in=params.getlist("level"))
            )

        if "status" in params:
            queryset = queryset.filter(status__in=params.getlist("status"))

        orderby = params.get("orderby", "-occurred_time")
        if orderby == "name":
            queryset = sorted(queryset, key=lambda t: t.namestr)
        elif orderby == "-name":
            queryset = sorted(queryset, key=lambda t: t.namestr, reverse=True)
        else:
            if orderby not in SecurityEventSearch.VALID_ORDER_BY:
                orderby = "-occurred_time"

            queryset = queryset.order_by(orderby, "pk")

        return queryset

    def get_data(self, context):
        site = context["site"]
        page_obj = context["page_obj"]
        paginator = context["paginator"]
        adjacent_pages = 2
        page_numbers = [
            n
            for n in range(
                page_obj.number - adjacent_pages, page_obj.number + adjacent_pages + 1
            )
            if n > 0 and n <= paginator.num_pages
        ]

        result = {
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "page": page_obj.number,
            "page_numbers": page_numbers,
            "has_next": page_obj.has_next(),
            "next_page_number": (
                page_obj.next_page_number() if page_obj.has_next() else None
            ),
            "has_previous": page_obj.has_previous(),
            "previous_page_number": (
                page_obj.previous_page_number() if page_obj.has_previous() else None
            ),
            "results": [
                {
                    "pk": event.pk,
                    "slug": site.uid,
                    "problem_name": (
                        event.problem.name
                        if event.problem
                        else event.event_rule_server.name
                    ),
                    "problem_url": (
                        reverse(
                            "event_rule_security_problem",
                            args=[site.uid, event.problem.id],
                        )
                        if event.problem
                        else reverse(
                            "event_rule_server",
                            args=[site.uid, event.event_rule_server.id],
                        )
                    ),
                    "pc_id": event.pc.id,
                    "occurred": event.occurred_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "reported": event.reported_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": event.get_status_display(),
                    "status_label": event.STATUS_TO_LABEL[event.status],
                    "level": EventLevels.LEVEL_TRANSLATIONS[
                        (
                            event.problem.level
                            if event.problem
                            else event.event_rule_server.level
                        )
                    ],
                    "level_label": EventLevels.LEVEL_TO_LABEL[
                        (
                            event.problem.level
                            if event.problem
                            else event.event_rule_server.level
                        )
                    ]
                    + "",
                    "pc_name": event.pc.name,
                    "pc_url": reverse("computer", args=[site.uid, event.pc.uid]),
                    "assigned_user": (
                        event.assigned_user.username if event.assigned_user else ""
                    ),
                    "assigned_user_url": (
                        reverse("user", args=[site.uid, event.assigned_user.username])
                        if event.assigned_user
                        else ""
                    ),
                    "summary": escape(event.summary),
                    "note": event.note,
                }
                for event in page_obj
            ],
        }

        return result


class SecurityEventsUpdate(SiteMixin, SuperAdminOrThisSiteMixin, ListView):
    http_method_names = ["post"]
    model = SecurityEvent

    def get_queryset(self):
        queryset = super().get_queryset()
        site = get_object_or_404(Site, uid=self.kwargs["slug"])
        params = self.request.POST
        ids = params.getlist("ids")
        queryset = queryset.filter(id__in=ids, pc__site=site)

        return queryset

    def post(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        params = self.request.POST

        status = params.get("status")
        assigned_user = params.get("assigned_user")
        note = params.get("note")

        queryset.update(status=status, assigned_user=assigned_user, note=note)

        return HttpResponse("OK")


documentation_menu_items = [
    ("", _("The administration site")),
    ("om_os2borgerpc_admin", _("About")),
    ("sites_overview", _("Sites overview")),
    ("status", _("Status")),
    ("computers", _("Computers")),
    ("groups", _("Groups")),
    ("wake_plans", _("On/Off schedules")),
    ("jobs", _("Jobs")),
    ("scripts", _("Scripts")),
    ("security_scripts", _("Security Scripts")),
    ("notifications", _("Notifications and offline rules")),
    ("users", _("Users")),
    ("configuration", _("Configurations")),
    ("changelogs", _("The News site")),
    ("api", "API"),
    ("creating_security_problems", _("Setting up security surveillance (PDF)")),
    ("", _("OS2borgerPC")),
    ("os2borgerpc_installation_guide", _("Installation Guide (PDF)")),
    ("os2borgerpc_installation_guide_old", _("Old installation guide (PDF)")),
    ("", _("OS2borgerPC Kiosk")),
    ("os2borgerpc_kiosk_installation_guide", _("Installation Guide")),
    ("os2borgerpc_kiosk_wifi_guide", _("Updating Wi-Fi setup")),
    ("", _("Audit")),
    ("audit_doc", _("FAQ (PDF)")),
    ("", _("Technical Documentation")),
    ("tech/os2borgerpc-image", _("OS2borgerPC Image")),
    ("tech/os2borgerpc-admin", _("OS2borgerPC Admin Site")),
    ("tech/os2borgerpc-server-image", _("OS2borgerPC Kiosk Image")),
    ("tech/os2borgerpc-client", _("OS2borgerPC Client")),
]


class DocView(TemplateView, LoginRequiredMixin):
    docname = "status"

    def template_exists(self, subpath):
        fullpath = os.path.join(settings.DOCUMENTATION_DIR, subpath)
        return os.path.isfile(fullpath)

    def get_context_data(self, **kwargs):  # noqa
        if "name" in self.kwargs:
            self.docname = self.kwargs["name"]
        else:
            # This will be mapped to documentation/index.html
            self.docname = "index"

        if self.docname.find("..") != -1:
            raise Http404

        # Try <docname>.html and <docname>/index.html
        name_templates = ["documentation/{0}.html", "documentation/{0}/index.html"]

        templatename = None
        for nt in name_templates:
            expanded = nt.format(self.docname)
            if self.template_exists(expanded):
                templatename = expanded
                break

        if templatename is None:
            raise Http404
        else:
            self.template_name = templatename

        context = super(DocView, self).get_context_data(**kwargs)
        context["docmenuitems"] = documentation_menu_items
        docnames = self.docname.split("/")

        # Returns the first site the user is a member of
        context["site"] = self.request.user.user_profile.sites.first()

        context["menu_active"] = docnames[0]

        # Get the links to the pdf files for the chosen language
        pdf_href = {
            "wake_plan_user_guide": "https://github.com/OS2borgerPC/admin-site/raw/development/admin_site"
            + "/static/docs/Wake_plan_user_guide",
            "os2borgerpc_installation_guide": "https://github.com/OS2borgerPC/image/raw/development/"
            + "docs/OS2BorgerPC_installation_guide",
            "os2borgerpc_installation_guide_old": "https://github.com/OS2borgerPC/image/raw/development/"
            + "docs/OS2BorgerPC_installation_guide_old",
            "creating_security_problems": "https://raw.githubusercontent.com/OS2borgerPC/admin-site/development/"
            + "admin_site/static/docs/OS2BorgerPC_security_rules",
            "audit_doc": "https://github.com/OS2borgerPC/admin-site/raw/development/admin_site"
            + "/static/docs/Audit_doc",
            "customer_admin_guide": "https://github.com/OS2borgerPC/admin-site/raw/development/admin_site"
            + "/static/docs/customer_admin_guide",
        }
        for key in pdf_href:
            pdf_href[key] += "_" + self.request.user.user_profile.language + ".pdf"
        context["pdf_href"] = pdf_href

        # Set heading according to chosen item
        current_heading = None
        for link, name in context["docmenuitems"]:
            if link == "":
                current_heading = name
            elif link == docnames[0]:
                context["docheading"] = current_heading
                break

        # Add a submenu if it exists
        submenu_template = "documentation/" + docnames[0] + "/__submenu__.html"
        if self.template_exists(submenu_template):
            context["submenu_template"] = submenu_template

        if len(docnames) > 1 and docnames[1]:
            # Don't allow direct access to submenus
            if docnames[1] == "__submenu__":
                raise Http404
            context["submenu_active"] = docnames[1]

        params = self.request.GET or self.request.POST
        back_link = params.get("back")
        if back_link is None:
            referer = self.request.META.get("HTTP_REFERER")
            if referer and referer.find("/documentation/") == -1:
                back_link = referer
        if back_link:
            context["back_link"] = back_link

        return context

class ImageVersionRedirect(RedirectView):
    def get_redirect_url(self, **kwargs):
        site = get_object_or_404(Site, uid=kwargs["slug"])

        return reverse(
            "images-product",
            kwargs={"slug": site.url, "product_id": Product.objects.first().id},
        )


class ImageVersionView(SiteMixin, SuperAdminOrThisSiteMixin, ListView):
    """Displays all of the image versions that this site has access to (i.e.,
    all versions released before the site's paid_for_access_until datestamp).
    """

    template_name = "system/site_images.html"
    model = ImageVersion
    selection_class = ImageVersion

    def get_context_data(self, **kwargs):
        context = super(ImageVersionView, self).get_context_data(**kwargs)

        site = get_object_or_404(Site, uid=self.kwargs["slug"])

        selected_product = get_object_or_404(Product, id=self.kwargs.get("product_id"))

        # If client's last pay date is set, exclude versions where
        # image release date > client's last pay date.
        if not site.customer.paid_for_access_until:
            versions_accessible_by_user = ImageVersion.objects.filter(
                product=selected_product
            ).order_by("-image_version")
        else:
            versions_accessible_by_user = (
                ImageVersion.objects.exclude(
                    release_date__gt=site.customer.paid_for_access_until
                )
                .filter(product=selected_product)
                .order_by("-image_version")
            )

        # If the product is multilang: Don't show images besides multilang for all languages besides danish
        user_language = self.request.user.user_profile.language
        if selected_product.multilang and user_language != "da":
            versions_accessible_by_user = versions_accessible_by_user.exclude(
                image_upload_multilang="#"
            )  # The hash symbol is the default for the field, indicating no file was uploaded

        products = Product.objects.all()

        # Swedish hacks until we do a proper translation of the database
        # TODO: Please remove this section!
        if user_language == "sv":
            selected_product.name = selected_product.name.replace(
                "OS2borgerPC", "Sambruk MedborgarPC"
            )
            for i in versions_accessible_by_user:
                i.product.name = i.product.name.replace(
                    "OS2borgerPC", "Sambruk MedborgarPC"
                )
            for p in products:
                p.name = p.name.replace("OS2borgerPC", "Sambruk MedborgarPC")

        context["selected_product"] = selected_product
        context["object_list"] = versions_accessible_by_user
        context["products"] = products
        context["user_language"] = user_language

        return context
