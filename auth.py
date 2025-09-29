from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", SCOPES
)

# This works even in older versions
creds = flow.run_local_server(port=0)

with open("credentials.json", "w") as f:
    f.write(creds.to_json())

print("✅ Credentials saved to credentials.json")