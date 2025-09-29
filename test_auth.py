from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

creds = Credentials.from_authorized_user_file("credentials.json")
youtube = build("youtube", "v3", credentials=creds)

request = youtube.channels().list(part="snippet", mine=True)
response = request.execute()
print(response)