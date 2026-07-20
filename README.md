# ByCS ViKo – Moderator-Beitritt

Automatisierter Beitritt zu ByCS-ViKo-Räumen (Visavid) als Moderator*in via Selenium.
Loggt sich per ByCS-SSO ein, betritt alle Dauerräume nacheinander (je eigener Tab)
und tritt mit hinterlegtem Anzeigenamen bei.

Schwesterprojekt zu `backup_courses.py` – gleicher Aufbau (Retry-Decorator,
`load_config`/`create_webdriver`, Logging).

## Voraussetzungen

- Python 3.10+
- Google Chrome + passender ChromeDriver

## Installation

```
pip install selenium python-dotenv
```

## Konfiguration

Datei: `config/.env.Viko`

| Variable            | Pflicht | Default | Beschreibung                                                        |
| ------------------- | ------- | ------- | ------------------------------------------------------------------- |
| `MEBIS_USERNAME`    | Ja      | –       | ByCS-Benutzername                                                   |
| `MEBIS_PASSWORD`    | Ja      | –       | ByCS-Passwort                                                       |
| `VIKO_DISPLAY_NAME` | Ja      | –       | Anzeigename im Raum (max. 40 Zeichen)                              |
| `VIKO_ROOM_NAME`    | Nein    | –       | Filter: nur Räume, deren Name diesen Text enthält                  |
| `VIKO_ROOM_URL`     | Nein    | –       | Direkter Raum-Link, überspringt Liste + Dialog (genau ein Raum)   |
| `VIKO_REUSE_NAME`   | Nein    | `True`  | Checkbox „Name bei nächster Sitzung wiederverwenden“              |
| `VIKO_MUTE_OUTPUT`  | Nein    | `False` | Checkbox „Ton-Ausgabe bei Betreten deaktivieren“ (stille Präsenz) |
| `MODE_HEADLESS`     | Nein    | `False` | `True` = ohne Fenster (Hintergrund-Bot)                            |
| `USE_FAKE_MEDIA`    | Nein    | `True`  | Fake-Kamera/-Mikro (nötig bei mehreren parallelen Räumen)         |
| `KEEP_OPEN`         | Nein    | `True`  | Alle Tabs offen halten bis Enter                                   |

## Nutzung

```
python scripts/viko_enter.py
```

Ablauf:

1. Login über die OIDC-Auth-URL (SSO, gleiche Session wie die Lernplattform)
2. Tab „Dauerhafte Räume“ öffnen, alle Räume ermitteln
3. Pro Raum in eigenem Tab: `Raum betreten` → ggf. `Als Moderator betreten` →
   Name eintragen, Checkboxen setzen → `Raum als Moderator betreten`
4. OK/FEHLER-Zusammenfassung; Tabs bleiben offen (`KEEP_OPEN=True`)

Ein fehlgeschlagener Raum blockiert die übrigen nicht.

## Selektor-Status

Verifiziert am echten DOM: Login-Formular, Raumliste + Tab, `/v/`-Beitrittsseite
(Namensfeld, beide Checkboxen, Submit-Button `test-login-button`).

Text-basiert (tolerant, wird übersprungen falls nicht vorhanden): der
Zwischendialog `Als Moderator betreten`.

Hinweis: Der finale Button heißt „Raum als Moderator **betreten**“ (nicht
„beitreten“) – die Selektoren sind entsprechend gesetzt.

## Sicherheit

`.env.Viko` enthält Login-Daten und darf **nicht** ins Repo. Siehe `.gitignore`.