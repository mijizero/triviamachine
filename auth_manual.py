from google_auth_oauthlib.flow import InstalledAppFlow
import pickle

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Use your downloaded client_secret.json
flow = InstalledAppFlow.from_client_secrets_file(
    "credentials.json",
    scopes=SCOPES
)

# Manually paste the code from the URL (everything after code=)
auth_code = input("Paste the authorization code from the URL here: ").strip()

# Fetch the token
creds = flow.fetch_token(code=auth_code)

# Save credentials for future uploads
with open("token.pickle", "wb") as token_file:
    pickle.dump(creds, token_file)

print("Authorization successful! Credentials saved to token.pickle")