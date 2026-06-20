import base64
import requests
import msal
from pathlib import Path
from src.config_manager import load_settings

GRAPH = 'https://graph.microsoft.com/v1.0'
SCOPE = ['https://graph.microsoft.com/.default']

INVOICE_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif'}


class EmailMonitor:
    def __init__(self):
        self.settings = load_settings()

    def _get_token(self) -> str:
        s = self.settings['email']
        app = msal.ConfidentialClientApplication(
            s['client_id'],
            authority=f"https://login.microsoftonline.com/{s['tenant_id']}",
            client_credential=s['client_secret']
        )
        result = app.acquire_token_silent(SCOPE, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=SCOPE)
        if 'access_token' not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error_description', result)}")
        return result['access_token']

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self._get_token()}', 'Content-Type': 'application/json'}

    def get_unread_invoice_emails(self) -> list:
        mailbox = self.settings['email']['mailbox']
        url = (f"{GRAPH}/users/{mailbox}/messages"
               f"?$filter=isRead eq false and hasAttachments eq true"
               f"&$orderby=receivedDateTime asc&$top=50"
               f"&$select=id,subject,from,receivedDateTime,hasAttachments")
        resp = requests.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.json().get('value', [])

    def get_invoice_attachments(self, message_id: str) -> list:
        mailbox = self.settings['email']['mailbox']
        url = f"{GRAPH}/users/{mailbox}/messages/{message_id}/attachments"
        resp = requests.get(url, headers=self._headers())
        resp.raise_for_status()
        all_attachments = resp.json().get('value', [])
        return [a for a in all_attachments if self._is_invoice_file(a.get('name', ''))]

    def download_attachment(self, message_id: str, attachment_id: str, save_path: str) -> str:
        mailbox = self.settings['email']['mailbox']
        url = f"{GRAPH}/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}"
        resp = requests.get(url, headers=self._headers())
        resp.raise_for_status()
        content = base64.b64decode(resp.json()['contentBytes'])
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, 'wb') as f:
            f.write(content)
        return save_path

    def mark_as_read(self, message_id: str):
        mailbox = self.settings['email']['mailbox']
        url = f"{GRAPH}/users/{mailbox}/messages/{message_id}"
        requests.patch(url, headers=self._headers(), json={'isRead': True})

    def move_to_processed(self, message_id: str):
        mailbox = self.settings['email']['mailbox']
        folder_name = self.settings['email'].get('processed_folder', 'AP Processed')
        folder_id = self._get_or_create_folder(mailbox, folder_name)
        url = f"{GRAPH}/users/{mailbox}/messages/{message_id}/move"
        requests.post(url, headers=self._headers(), json={'destinationId': folder_id})

    def _get_or_create_folder(self, mailbox: str, folder_name: str) -> str:
        url = f"{GRAPH}/users/{mailbox}/mailFolders?$filter=displayName eq '{folder_name}'"
        resp = requests.get(url, headers=self._headers())
        folders = resp.json().get('value', [])
        if folders:
            return folders[0]['id']
        resp = requests.post(
            f"{GRAPH}/users/{mailbox}/mailFolders",
            headers=self._headers(),
            json={'displayName': folder_name}
        )
        resp.raise_for_status()
        return resp.json()['id']

    def _is_invoice_file(self, filename: str) -> bool:
        return Path(filename).suffix.lower() in INVOICE_EXTENSIONS

    def test_connection(self) -> tuple[bool, str]:
        try:
            mailbox = self.settings['email']['mailbox']
            token = self._get_token()
            url = f"{GRAPH}/users/{mailbox}/mailFolders/inbox"
            resp = requests.get(url, headers={'Authorization': f'Bearer {token}'})
            resp.raise_for_status()
            return True, "Connection successful"
        except Exception as e:
            return False, str(e)
