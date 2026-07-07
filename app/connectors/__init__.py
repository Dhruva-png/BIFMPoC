"""
External intake/filing connectors.

Per the requirements doc (Section 2/3), instruction forms arrive via email
or as a walk-in, and get uploaded to a SharePoint date folder (the
"submission folder") which the team then segregates and files into
Captured/Approved/Rejected subfolders. This package gives the app two
extra intake channels on top of manual upload / a local folder:

  sharepoint_client.py - lists and downloads PDFs sitting in a SharePoint
                          document library folder (Microsoft Graph API),
                          and can push filed documents back into the
                          correct SharePoint subfolder.
  gmail_client.py       - lists and downloads PDF attachments from a Gmail
                          inbox/label matching a search query (Gmail API).

Both are optional: if not configured (no credentials in the environment),
`is_configured()` returns False and the corresponding sidebar option is
disabled rather than the app failing.
"""
