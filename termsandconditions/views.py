"""Django Views for the termsandconditions module"""

from django.contrib.auth.models import User
from django.db import IntegrityError

from django.core.cache import cache

from .forms import UserTermsAndConditionsModelForm, EmailTermsForm
from .models import TermsAndConditions, UserTermsAndConditions
from django.conf import settings
from django.core.mail import send_mail
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.utils.translation import gettext as _
from django.views.generic import DetailView, CreateView, FormView
from django.template.loader import get_template
from django.utils.encoding import iri_to_uri
import logging
from smtplib import SMTPException

LOGGER = logging.getLogger(name="termsandconditions")
DEFAULT_TERMS_BASE_TEMPLATE = "base.html"
DEFAULT_TERMS_IP_HEADER_NAME = "REMOTE_ADDR"
TERMS_CACHE_SECONDS = getattr(settings, "TERMS_CACHE_SECONDS", 30)


class GetTermsViewMixin:
    """Checks URL parameters for slug and/or version to pull the right TermsAndConditions object"""

    def get_terms(self, kwargs):
        """Checks URL parameters for slug and/or version to pull the right TermsAndConditions object"""

        slug = kwargs.get("slug")
        version = kwargs.get("version")

        if slug and version:
            terms = [
                TermsAndConditions.objects.filter(
                    slug=slug, version_number=version
                ).latest("date_active")
            ]
        elif slug:
            terms = [TermsAndConditions.get_active(slug)]
        else:
            # Return a list of not agreed to terms for the current user for the list view
            terms = TermsAndConditions.get_active_terms_not_agreed_to(self.request.user)
        return terms

    def get_return_to(self, from_dict):
        return_to = from_dict.get("returnTo", "/")

        if self.is_safe_url(return_to):
            # Django recommends to use this together with the helper above
            return iri_to_uri(return_to)

        LOGGER.debug(f"Unsafe URL found: {return_to}")
        return "/"

    def is_safe_url(self, url):
        # In Django 3.0 is_safe_url is renamed, so we import conditionally:
        # https://docs.djangoproject.com/en/3.2/releases/3.0/#id3
        try:
            from django.utils.http import url_has_allowed_host_and_scheme
        except ImportError:
            from django.utils.http import (
                is_safe_url as url_has_allowed_host_and_scheme,
            )

        return url_has_allowed_host_and_scheme(url, settings.ALLOWED_HOSTS)


class AcceptTermsView(CreateView, GetTermsViewMixin):
    """
    Terms and Conditions Acceptance view

    url: /terms/accept
    """

    model = UserTermsAndConditions
    form_class = UserTermsAndConditionsModelForm
    template_name = "termsandconditions/tc_accept_terms.html"

    def get_context_data(self, **kwargs):
        """Pass additional context data"""
        context = super().get_context_data(**kwargs)
        context["terms_base_template"] = getattr(
            settings, "TERMS_BASE_TEMPLATE", DEFAULT_TERMS_BASE_TEMPLATE
        )
        return context

    def get_initial(self):
        """Override of CreateView method, queries for which T&C to accept and catches returnTo from URL"""
        LOGGER.debug("termsandconditions.views.AcceptTermsView.get_initial")

        terms = self.get_terms(self.kwargs)
        return_to = self.get_return_to(self.request.GET)

        return {"terms": terms, "returnTo": return_to}

    def post(self, request, *args, **kwargs):
        """
        Handles POST request.
        """
        return_url = self.get_return_to(self.request.POST)
        terms_ids = request.POST.getlist("terms")

        if not terms_ids:  # pragma: nocover
            return HttpResponseRedirect(return_url)

        if request.user.is_authenticated:
            user = request.user
        else:
            # Get user out of saved pipeline from django-socialauth
            if "partial_pipeline" in request.session:
                user_pk = request.session["partial_pipeline"]["kwargs"]["user"]["pk"]
                user = User.objects.get(id=user_pk)
            else:
                return HttpResponseRedirect("/")

        store_ip_address = getattr(settings, "TERMS_STORE_IP_ADDRESS", True)
        if store_ip_address:
            ip_address = request.META.get(
                getattr(settings, "TERMS_IP_HEADER_NAME", DEFAULT_TERMS_IP_HEADER_NAME)
            )
            if "," in ip_address:
                ip_address = ip_address.split(",")[0].strip()
        else:
            ip_address = ""

        for terms_id in terms_ids:
            try:
                new_user_terms = UserTermsAndConditions(
                    user=user,
                    terms=TermsAndConditions.objects.get(pk=int(terms_id)),
                    ip_address=ip_address,
                )
                new_user_terms.save()
                not_agreed_terms = (
                    TermsAndConditions.get_active_terms_list()
                    .exclude(
                        userterms__in=UserTermsAndConditions.objects.filter(user=user)
                    )
                    .order_by("slug")
                )
                cache.set(
                    "tandc.not_agreed_terms_" + user.get_username(),
                    not_agreed_terms,
                    TERMS_CACHE_SECONDS,
                )
            except IntegrityError:  # pragma: nocover
                pass

        return HttpResponseRedirect(return_url)


class EmailTermsView(FormView, GetTermsViewMixin):
    """
    Email Terms and Conditions View

    url: /terms/email
    """

    template_name = "termsandconditions/tc_email_terms_form.html"

    form_class = EmailTermsForm

    def get_context_data(self, **kwargs):
        """Pass additional context data"""
        context = super().get_context_data(**kwargs)
        context["terms_base_template"] = getattr(
            settings, "TERMS_BASE_TEMPLATE", DEFAULT_TERMS_BASE_TEMPLATE
        )
        return context

    def get_initial(self):
        """Override of CreateView method, queries for which T&C send, catches returnTo from URL"""
        LOGGER.debug("termsandconditions.views.EmailTermsView.get_initial")

        terms = self.get_terms(self.kwargs)

        return_to = self.get_return_to(self.request.GET)

        return {"terms": terms, "returnTo": return_to}

    def form_valid(self, form):
        """Override of CreateView method, sends the email."""
        LOGGER.debug("termsandconditions.views.EmailTermsView.form_valid")

        template = get_template("termsandconditions/tc_email_terms.html")
        template_rendered = template.render({"terms": form.cleaned_data.get("terms")})

        LOGGER.debug("Email Terms Body:")
        LOGGER.debug(template_rendered)

        try:
            send_mail(
                form.cleaned_data.get("email_subject", _("Terms")),
                template_rendered,
                settings.DEFAULT_FROM_EMAIL,
                [form.cleaned_data.get("email_address")],
                fail_silently=False,
            )
            messages.add_message(
                self.request, messages.INFO, _("Terms and Conditions Sent.")
            )
        except SMTPException:  # pragma: no cover
            messages.add_message(
                self.request,
                messages.ERROR,
                _("An Error Occurred Sending Your Message."),
            )

        self.success_url = self.get_return_to(form.cleaned_data)

        return super().form_valid(form)

    def form_invalid(self, form):
        """Override of CreateView method, logs invalid email form submissions."""
        LOGGER.debug("Invalid Email Form Submitted")
        messages.add_message(self.request, messages.ERROR, _("Invalid Email Address."))
        return super().form_invalid(form)


class TermsView(DetailView, GetTermsViewMixin):
    """
    View Unaccepted Terms and Conditions View

    url: /terms/
    """

    template_name = "termsandconditions/tc_view_terms.html"
    context_object_name = "terms_list"

    def get_context_data(self, **kwargs):
        """Pass additional context data"""
        context = super().get_context_data(**kwargs)
        context["terms_base_template"] = getattr(
            settings, "TERMS_BASE_TEMPLATE", DEFAULT_TERMS_BASE_TEMPLATE
        )
        return context

    def get_object(self, queryset=None):
        """Override of DetailView method, queries for which T&C to return"""
        LOGGER.debug("termsandconditions.views.TermsView.get_object")
        return self.get_terms(self.kwargs)


class TermsActiveView(TermsView):
    """
    View Active Terms and Conditions View

    url: /terms/active/
    """

    def get_object(self, queryset=None):
        """Override of DetailView method, queries for which T&C to return"""
        LOGGER.debug("termsandconditions.views.AllTermsView.get_object")
        return TermsAndConditions.get_active_terms_list()
