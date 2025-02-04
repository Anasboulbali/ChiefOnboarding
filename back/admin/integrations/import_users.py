from admin.integrations.exceptions import KeyIsNotInDataError
from django.contrib.auth import get_user_model
from organization.models import Organization

from admin.integrations.utils import get_value_from_notation
from django.utils.translation import gettext_lazy as _
from admin.integrations.exceptions import GettingUsersError


class ImportUser:
    """
    Extension of the `Integration` model. This part is only used to get users
    from a third party API endpoint and format them in a way that we can proccess
    them.
    """

    def __init__(self, integration):
        self.integration = integration

    def extract_users_from_list_response(self, response):
        # Building list of users from response. Dig into response to get to the users.
        data_from = self.integration.manifest["data_from"]

        try:
            users = get_value_from_notation(data_from, response.json())
        except KeyError:
            # This is unlikely to go wrong - only when api changes or when
            # configs are being setup
            raise KeyIsNotInDataError(
                _("Notation '%(notation)s' not in %(response)s")
                % {
                    "notation": data_from,
                    "response": self.integration.clean_response(response.json()),
                }
            )
        data_structure = self.integration.manifest["data_structure"]
        user_details = []
        for user_data in users:
            user = {}
            for prop, notation in data_structure.items():
                try:
                    user[prop] = get_value_from_notation(notation, user_data)
                except KeyError:
                    # This is unlikely to go wrong - only when api changes or when
                    # configs are being setup
                    raise KeyIsNotInDataError(
                        _("Notation '%(notation)s' not in %(response)s")
                        % {
                            "notation": notation,
                            "response": self.integration.clean_response(user_data),
                        }
                    )
            user_details.append(user)
        return user_details

    def get_next_page(self, response):
        # Some apis give us back a full URL, others just a token. If we get a full URL,
        # follow that, if we get a token, then also specify the next_page. The token
        # gets placed through the NEXT_PAGE_TOKEN variable.

        # taken from response - full url including params for next page
        next_page_from = self.integration.manifest.get("next_page_from")

        # build up url ourself based on hardcoded url + token for next part
        next_page_token_from = self.integration.manifest.get("next_page_token_from")
        # hardcoded in manifest
        next_page = self.integration.manifest.get("next_page")

        # skip if none provided
        if not next_page_from and not (next_page_token_from and next_page):
            return

        if next_page_from:
            try:
                return get_value_from_notation(next_page_from, response.json())
            except KeyError:
                # next page was not provided anymore, so we are done
                return

        # Build next url from next_page and next_page_token_from
        try:
            token = get_value_from_notation(next_page_token_from, response.json())
        except KeyError:
            # next page token was not provided anymore, so we are done
            return

        # Replace token variable with real token
        self.integration.params["NEXT_PAGE_TOKEN"] = token
        return self.integration._replace_vars(next_page)

    def get_import_user_candidates(self, user):
        success, response = self.integration.execute(user, {})
        if not success:
            raise GettingUsersError(self.integration.clean_response(response))

        users = self.extract_users_from_list_response(response)

        amount_pages_to_fetch = self.integration.manifest.get(
            "amount_pages_to_fetch", 5
        )
        fetched_pages = 1
        while amount_pages_to_fetch != fetched_pages:
            # End everything if next page does not exist
            next_page_url = self.get_next_page(response)
            if next_page_url is None:
                break

            success, response = self.integration.run_request(
                {"method": "GET", "url": next_page_url}
            )
            if not success:
                raise GettingUsersError(
                    _("Paginated URL fetch: %(response)s")
                    % {"response": self.integration.clean_response(response)}
                )

            # Check if there are any new results. Google could send no users back
            try:
                data_from = self.integration.manifest["data_from"]
                get_value_from_notation(data_from, response.json())
            except KeyError:
                break

            users += self.extract_users_from_list_response(response)
            fetched_pages += 1

        # Remove users that are already in the system or have been ignored
        existing_user_emails = list(
            get_user_model().objects.all().values_list("email", flat=True)
        )
        ignored_user_emails = Organization.objects.get().ignored_user_emails
        excluded_emails = (
            existing_user_emails + ignored_user_emails + ["", None]
        )  # also add blank emails to ignore

        user_candidates = [
            user_data
            for user_data in users
            if user_data.get("email", "") not in excluded_emails
        ]

        return user_candidates
