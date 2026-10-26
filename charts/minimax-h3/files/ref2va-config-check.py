import sys

try:
    from h3_service.config import REF2VA_TURBO_CONFIG_VERSION
except ImportError:
    REF2VA_TURBO_CONFIG_VERSION = None

if REF2VA_TURBO_CONFIG_VERSION != 1:
    sys.exit(
        "Incompatible H3 Server image: Ref2VA requires Turbo config protocol 1 "
        "for both Turbo and base mode. Rebuild the Server from this release "
        "and update server.image.tag / H3_SERVER_IMAGE."
    )
