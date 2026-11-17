API_LINK = "https://api.aino.world"

# Aino's Mixpanel project token: the public one app.aino.world ships as NEXT_PUBLIC_MIXPANEL_TOKEN.
# Empty turns usage events off.
MIXPANEL_TOKEN = "1faae876ca4d67ef58cc45125ce80347"

# UTM tags let Aino's analytics attribute a sign-up or a purchase to the plugin.
SIGNUP_URL = (
    "https://app.aino.world/auth?utm_source=qgis&utm_medium=plugin&utm_campaign=signup"
)
# Opens the paywall that fits the account, then points the user back to QGIS.
ADD_CREDITS_URL = (
    "https://app.aino.world/integrations/qgis/credits"
    "?utm_source=qgis&utm_medium=plugin&utm_campaign=add_credits"
)
