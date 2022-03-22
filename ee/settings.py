"""
Django settings for PostHog Enterprise Edition.
"""
import os
from typing import Dict, List

from posthog.kafka_client.topics import KAFKA_EVENTS_PLUGIN_INGESTION as DEFAULT_KAFKA_EVENTS_PLUGIN_INGESTION
from posthog.settings import AUTHENTICATION_BACKENDS, SITE_URL, TEST, get_from_env
from posthog.utils import print_warning, str_to_bool

# Zapier REST hooks
HOOK_EVENTS: Dict[str, str] = {
    # "event_name": "App.Model.Action" (created/updated/deleted)
    "action_defined": "posthog.Action.created_custom",
    "action_performed": "posthog.Action.performed",
    "annotation_created": "posthog.Annotation.created_custom",
}
HOOK_FINDER = "ee.models.hook.find_and_fire_hook"
HOOK_DELIVERER = "ee.models.hook.deliver_hook_wrapper"

# SAML
SAML_CONFIGURED = False
SAML_ENFORCED = False
SOCIAL_AUTH_SAML_SP_ENTITY_ID = SITE_URL
SOCIAL_AUTH_SAML_SECURITY_CONFIG = {
    "wantAttributeStatement": False,  # AttributeStatement is optional in the specification
}
# Attributes below are required for the SAML integration from social_core to work properly
SOCIAL_AUTH_SAML_SP_PUBLIC_CERT = ""
SOCIAL_AUTH_SAML_SP_PRIVATE_KEY = ""
SOCIAL_AUTH_SAML_ORG_INFO = {"en-US": {"name": "posthog", "displayname": "PostHog", "url": "https://posthog.com"}}
SOCIAL_AUTH_SAML_TECHNICAL_CONTACT = {"givenName": "PostHog Support", "emailAddress": "hey@posthog.com"}
SOCIAL_AUTH_SAML_SUPPORT_CONTACT = SOCIAL_AUTH_SAML_TECHNICAL_CONTACT


# Set settings only if SAML is enabled
if os.getenv("SAML_ENTITY_ID") and os.getenv("SAML_ACS_URL") and os.getenv("SAML_X509_CERT"):
    SAML_CONFIGURED = True
    AUTHENTICATION_BACKENDS = AUTHENTICATION_BACKENDS + [
        "social_core.backends.saml.SAMLAuth",
    ]
    SOCIAL_AUTH_SAML_ENABLED_IDPS = {
        "posthog_custom": {
            "entity_id": get_from_env("SAML_ENTITY_ID", optional=True),
            "url": get_from_env("SAML_ACS_URL", optional=True),
            "x509cert": get_from_env("SAML_X509_CERT", optional=True),
            "attr_user_permanent_id": get_from_env("SAML_ATTR_PERMANENT_ID", "name_id"),
            "attr_first_name": get_from_env("SAML_ATTR_FIRST_NAME", "first_name"),
            "attr_last_name": get_from_env("SAML_ATTR_LAST_NAME", "last_name"),
            "attr_email": get_from_env("SAML_ATTR_EMAIL", "email"),
        },
    }

    # DEPRECATED: `SAML_ENFORCED` attribute is deprecated in favor of `SSO_ENFORCEMENT` and will be removed in 1.35.0 onwards.
    SAML_ENFORCED = get_from_env("SAML_ENFORCED", False, type_cast=str_to_bool)
    if SAML_ENFORCED:
        print_warning(["`SAML_ENFOCED` attribute has been deprecated. Please use `SSO_ENFORCEMENT` instead."])


# SSO
SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = os.getenv("SOCIAL_AUTH_GOOGLE_OAUTH2_KEY")
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = os.getenv("SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET")
if "SOCIAL_AUTH_GOOGLE_OAUTH2_WHITELISTED_DOMAINS" in os.environ:
    SOCIAL_AUTH_GOOGLE_OAUTH2_WHITELISTED_DOMAINS: List[str] = os.environ[
        "SOCIAL_AUTH_GOOGLE_OAUTH2_WHITELISTED_DOMAINS"
    ].split(",")

AUTHENTICATION_BACKENDS = AUTHENTICATION_BACKENDS + [
    "social_core.backends.google.GoogleOAuth2",
]

SSO_ENFORCEMENT = get_from_env("SSO_ENFORCEMENT", "saml" if SAML_ENFORCED else None, optional=True)

# ClickHouse and Kafka
KAFKA_ENABLED = not TEST

# Schedule to run column materialization on. Follows crontab syntax.
# Use empty string to prevent from materializing
MATERIALIZE_COLUMNS_SCHEDULE_CRON = get_from_env("MATERIALIZE_COLUMNS_SCHEDULE_CRON", "0 5 * * SAT")
# Minimum query time before a query if considered for optimization by adding materialized columns
MATERIALIZE_COLUMNS_MINIMUM_QUERY_TIME = get_from_env("MATERIALIZE_COLUMNS_MINIMUM_QUERY_TIME", 3000, type_cast=int)
# How many hours backwards to look for queries to optimize
MATERIALIZE_COLUMNS_ANALYSIS_PERIOD_HOURS = get_from_env(
    "MATERIALIZE_COLUMNS_ANALYSIS_PERIOD_HOURS", 7 * 24, type_cast=int
)
# How big of a timeframe to backfill when materializing event properties. 0 for no backfilling
MATERIALIZE_COLUMNS_BACKFILL_PERIOD_DAYS = get_from_env("MATERIALIZE_COLUMNS_BACKFILL_PERIOD_DAYS", 90, type_cast=int)
# Maximum number of columns to materialize at once. Avoids running into resource bottlenecks (storage + ingest + backfilling).
MATERIALIZE_COLUMNS_MAX_AT_ONCE = get_from_env("MATERIALIZE_COLUMNS_MAX_AT_ONCE", 10, type_cast=int)

# Topic to write events to between clickhouse
KAFKA_EVENTS_PLUGIN_INGESTION_TOPIC: str = os.getenv(
    "KAFKA_EVENTS_PLUGIN_INGESTION_TOPIC", DEFAULT_KAFKA_EVENTS_PLUGIN_INGESTION
)
