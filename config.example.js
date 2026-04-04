/**
 * Browser Gmail demo — copy this file to `config.local.js` and fill in real values.
 *
 *   cp config.example.js config.local.js
 *
 * `config.local.js` is gitignored. Never commit it.
 *
 * Values come from Google Cloud Console → APIs & Services → Credentials:
 * - clientId: OAuth 2.0 Client ID of type **Web application** (not Desktop).
 * - apiKey: an API key for the same project (restrict it by HTTP referrer in production).
 */
window.GMAIL_WEB_CONFIG = {
  clientId: 'YOUR_WEB_CLIENT_ID.apps.googleusercontent.com',
  apiKey: 'YOUR_BROWSER_API_KEY',
};
