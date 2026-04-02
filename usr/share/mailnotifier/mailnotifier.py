#!/usr/bin/env python3
"""
mailnotifier.py — Secure IMAP Mail Notifier for Linux Mint / Cinnamon
"""

import gi
gi.require_version("Gtk", "3.0")

from gi.repository import Gtk, GLib
import imaplib
import subprocess
import threading
import configparser
import os
import re
import shlex
import socket
import sys
import logging
from pathlib import Path

# ── SecretService (GNOME Keyring) ────────────────────────────────────────────
try:
    import secretstorage
    SECRETSTORAGE_AVAILABLE = True
except ImportError:
    SECRETSTORAGE_AVAILABLE = False

# ── Konstanten ────────────────────────────────────────────────────────────────
CONFIG_DIR             = Path.home() / ".config" / "mailnotifier"
CONFIG_FILE            = CONFIG_DIR  / "mailnotifier.ini"
LOG_DIR                = Path.home() / ".local" / "share" / "mailnotifier"
ICON_DIR               = Path("/usr/share/mailnotifier")
ICON_GREY              = str(ICON_DIR / "icon_grey.svg")
ICON_BLUE              = str(ICON_DIR / "icon_blue.svg")
KEYRING_SERVICE        = "mailnotifier"
KEYRING_KEY_PASSWORD   = "imap_password"
KEYRING_KEY_MAILCLIENT = "mail_client"
IMAP_TIMEOUT           = 15
AUTOSTART_FILE         = Path.home() / ".config" / "autostart" / "mailnotifier.desktop"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(LOG_DIR, 0o700)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "mailnotifier.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# EINGABEVALIDIERUNG
# ══════════════════════════════════════════════════════════════════════════════

def validate_hostname(hostname: str) -> bool:
    if not hostname or len(hostname) > 253:
        return False
    try:
        socket.inet_aton(hostname)
        return True
    except socket.error:
        pass
    pattern = re.compile(
        r"^(?:[a-zA-Z0-9]"
        r"(?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+"
        r"[a-zA-Z]{2,}$"
    )
    return bool(pattern.match(hostname))


def validate_port(port_value) -> int:
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        raise ValueError(f"Port ist keine Zahl: {port_value!r}")
    if not (1 <= port <= 65535):
        raise ValueError(f"Port außerhalb des gültigen Bereichs (1–65535): {port}")
    return port


def validate_folder(folder: str) -> bool:
    if not folder or len(folder) > 255:
        return False
    forbidden = set(';\'"\\`$&|<>(){}!*?~^')
    return not any(c in forbidden for c in folder)


def validate_mail_client(client_path: str) -> list:
    if not client_path or not client_path.strip():
        raise ValueError("Kein Mail-Client-Pfad angegeben.")
    try:
        parts = shlex.split(client_path.strip())
    except ValueError as e:
        raise ValueError(f"Ungültiger Befehl (Syntaxfehler): {e}")
    if not parts:
        raise ValueError("Leerer Mail-Client-Befehl nach Parsing.")

    executable = parts[0]

    if os.path.isabs(executable):
        if not os.path.isfile(executable):
            raise ValueError(f"Programm nicht gefunden:\n{executable}")
        if not os.access(executable, os.X_OK):
            raise ValueError(f"Programm nicht ausführbar:\n{executable}")
        return parts

    result = subprocess.run(
        ["which", executable], capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(
            f"Programm '{executable}' nicht im PATH gefunden.\n"
            "Bitte den vollständigen Pfad angeben."
        )
    return parts


# ══════════════════════════════════════════════════════════════════════════════
# PASSWORT-VERWALTUNG — GNOME KEYRING
# ══════════════════════════════════════════════════════════════════════════════

class PasswordManager:

    @staticmethod
    def _get_collection():
        conn = secretstorage.dbus_init()
        col  = secretstorage.get_default_collection(conn)
        if col.is_locked():
            col.unlock()
        return col

    @staticmethod
    def save(key: str, value: str, label: str) -> None:
        if not SECRETSTORAGE_AVAILABLE:
            PasswordManager._fallback_warn()
            return
        try:
            col = PasswordManager._get_collection()
            col.create_item(
                f"{KEYRING_SERVICE} — {label}",
                {"service": KEYRING_SERVICE, "key": key},
                value,
                replace=True,
            )
            log.info(f"Keyring: '{label}' gespeichert.")
        except Exception as e:
            log.error(f"Keyring-Fehler beim Speichern von '{label}': {e}")
            raise

    @staticmethod
    def load(key: str) -> str | None:
        if not SECRETSTORAGE_AVAILABLE:
            return None
        try:
            col   = PasswordManager._get_collection()
            items = list(col.search_items(
                {"service": KEYRING_SERVICE, "key": key}
            ))
            if items:
                return items[0].get_secret().decode("utf-8")
        except Exception as e:
            log.error(f"Keyring-Fehler beim Laden von '{key}': {e}")
        return None

    @staticmethod
    def delete(key: str) -> None:
        if not SECRETSTORAGE_AVAILABLE:
            return
        try:
            col = PasswordManager._get_collection()
            for item in col.search_items(
                {"service": KEYRING_SERVICE, "key": key}
            ):
                item.delete()
            log.info(f"Keyring: '{key}' gelöscht.")
        except Exception as e:
            log.error(f"Keyring-Fehler beim Löschen von '{key}': {e}")

    @staticmethod
    def _fallback_warn():
        log.warning(
            "secretstorage nicht verfügbar! "
            "Bitte installieren: sudo apt install python3-secretstorage"
        )


# ══════════════════════════════════════════════════════════════════════════════
# KONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)


def load_config() -> configparser.ConfigParser:
    ensure_config_dir()
    config = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        current_mode = oct(os.stat(CONFIG_FILE).st_mode)[-3:]
        if current_mode not in ("600", "400"):
            os.chmod(CONFIG_FILE, 0o600)
        config.read(CONFIG_FILE)
    if "IMAP" not in config:
        config["IMAP"] = {
            "server":         "",
            "port":           "993",
            "username":       "",
            "folder":         "INBOX",
            "check_interval": "5",
            "autostart":      "true",
        }
    return config


def save_config(config: configparser.ConfigParser) -> None:
    ensure_config_dir()
    for sensitive_key in ("password", "mail_client"):
        if "IMAP" in config and sensitive_key in config["IMAP"]:
            del config["IMAP"][sensitive_key]
    with open(CONFIG_FILE, "w") as f:
        config.write(f)
    os.chmod(CONFIG_FILE, 0o600)
    log.info("Konfiguration gespeichert (ohne sensible Daten).")


# ══════════════════════════════════════════════════════════════════════════════
# IMAP MAIL-CHECK
# ══════════════════════════════════════════════════════════════════════════════

def check_mail(config: configparser.ConfigParser) -> dict | None:
    """
    Verbindet sich per IMAP und gibt ein Dict zurück:
      {
        "new_count":  int,   # Anzahl Mails mit UID > last_known_uid
        "total_unseen": int, # Gesamtzahl ungelesener Mails
        "max_uid":    int,   # Höchste UID im UNSEEN-Set (zum Speichern)
      }
    Gibt None zurück bei Verbindungsfehlern.
    """
    cfg      = config["IMAP"]
    server   = cfg.get("server",   "").strip()
    username = cfg.get("username", "").strip()
    folder   = cfg.get("folder",   "INBOX").strip()
    password = PasswordManager.load(KEYRING_KEY_PASSWORD)

    if not validate_hostname(server):
        log.error(f"Ungültiger IMAP-Server: {server!r}")
        return None
    try:
        port = validate_port(cfg.get("port", "993"))
    except ValueError as e:
        log.error(f"Port-Validierung: {e}")
        return None
    if not validate_folder(folder):
        log.error(f"Ungültiger Ordnername: {folder!r}")
        return None
    if not username or not password:
        log.warning("IMAP-Zugangsdaten unvollständig.")
        return None

    try:
        mail = imaplib.IMAP4_SSL(host=server, port=port, timeout=IMAP_TIMEOUT)
        mail.login(username, password)

        status, _ = mail.select(f'"{folder}"', readonly=True)
        if status != "OK":
            log.error(f"IMAP SELECT fehlgeschlagen: {folder!r}")
            mail.logout()
            return None

        # ── Alle UNSEEN-UIDs holen ─────────────────────────────────────
        status, data = mail.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            log.error("UID SEARCH UNSEEN fehlgeschlagen.")
            mail.logout()
            return None

        raw = data[0].decode() if isinstance(data[0], bytes) else (data[0] or "")
        unseen_uids = [int(u) for u in raw.split() if u.strip().isdigit()]

        mail.logout()

        total_unseen = len(unseen_uids)
        max_uid      = max(unseen_uids) if unseen_uids else 0

        log.info(
            f"UNSEEN gesamt: {total_unseen}, "
            f"höchste UID: {max_uid}"
        )
        return {
            "unseen_uids":   unseen_uids,
            "total_unseen":  total_unseen,
            "max_uid":       max_uid,
        }

    except imaplib.IMAP4.error as e:
        log.error(f"IMAP-Fehler: {e}")
    except socket.timeout:
        log.error(f"IMAP-Timeout nach {IMAP_TIMEOUT}s.")
    except socket.gaierror as e:
        log.error(f"DNS-Fehler für {server!r}: {e}")
    except ConnectionRefusedError:
        log.error(f"Verbindung zu {server}:{port} abgelehnt.")
    except OSError as e:
        log.error(f"Netzwerkfehler: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIL-CLIENT STARTEN
# ══════════════════════════════════════════════════════════════════════════════

def open_mail_client() -> None:
    client_path = PasswordManager.load(KEYRING_KEY_MAILCLIENT)
    if not client_path:
        log.warning("Kein Mail-Client im Keyring konfiguriert.")
        show_error_dialog(
            "Kein Mail-Client konfiguriert.\n\n"
            "Bitte unter Einstellungen → Mail-Client-Befehl eintragen.\n\n"
            "Beispiel für Chrome-WebApp:\n"
            "/opt/google/chrome/google-chrome "
            "--profile-directory=Default "
            "--app-id=iokmfmbcohpblmmofiolgddnohnlaonk"
        )
        return
    try:
        args = validate_mail_client(client_path)
        subprocess.Popen(
            args,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        log.info(f"Mail-Client gestartet: {args[0]}")
    except ValueError as e:
        log.error(f"Mail-Client-Fehler: {e}")
        show_error_dialog(str(e))


# ══════════════════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN
# ══════════════════════════════════════════════════════════════════════════════

def show_error_dialog(message: str) -> None:
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text="Mailnotifier — Fehler",
    )
    dialog.format_secondary_text(message)
    dialog.run()
    dialog.destroy()


def set_autostart(enabled: bool) -> None:
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Mailnotifier\n"
            "Exec=mailnotifier\n"
            "Icon=mailnotifier\n"
            "Hidden=false\n"
            "NoDisplay=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        AUTOSTART_FILE.write_text(content)
        os.chmod(AUTOSTART_FILE, 0o644)
        log.info("Autostart aktiviert.")
    else:
        if AUTOSTART_FILE.exists():
            AUTOSTART_FILE.unlink()
        log.info("Autostart deaktiviert.")


# ══════════════════════════════════════════════════════════════════════════════
# EINSTELLUNGSDIALOG
# ══════════════════════════════════════════════════════════════════════════════

class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent, config):
        super().__init__(
            title="Mailnotifier — Einstellungen",
            transient_for=parent,
        )
        self.set_default_size(500, 520)
        self.config = config

        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE,   Gtk.ResponseType.OK,
        )

        box  = self.get_content_area()
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=16)
        box.add(grid)

        def add_row(label_text, widget, row):
            label = Gtk.Label(label=label_text, xalign=0)
            label.set_width_chars(18)
            grid.attach(label,  0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)

        cfg = config["IMAP"]

        self.entry_server   = Gtk.Entry(text=cfg.get("server",         ""))
        self.entry_port     = Gtk.Entry(text=cfg.get("port",           "993"))
        self.entry_user     = Gtk.Entry(text=cfg.get("username",       ""))
        self.entry_password = Gtk.Entry(text="", visibility=False)
        self.entry_folder   = Gtk.Entry(text=cfg.get("folder",         "INBOX"))
        self.entry_interval = Gtk.Entry(text=cfg.get("check_interval", "5"))
        self.entry_client   = Gtk.Entry(text="")
        self.chk_autostart  = Gtk.CheckButton(label="Autostart aktivieren")
        self.chk_autostart.set_active(cfg.getboolean("autostart", True))

        self.entry_server.set_placeholder_text("z.B. mail.example.com")
        self.entry_password.set_placeholder_text(
            "Wird sicher im GNOME Keyring gespeichert"
        )
        self.entry_client.set_placeholder_text(
            "/opt/google/chrome/google-chrome --app-id=..."
        )

        stored_pw     = PasswordManager.load(KEYRING_KEY_PASSWORD)
        stored_client = PasswordManager.load(KEYRING_KEY_MAILCLIENT)

        if stored_pw:
            self.entry_password.set_text(stored_pw)
        if stored_client:
            self.entry_client.set_text(stored_client)

        add_row("IMAP-Server:",        self.entry_server,   0)
        add_row("Port:",               self.entry_port,     1)
        add_row("Benutzername:",       self.entry_user,     2)
        add_row("Passwort:",           self.entry_password, 3)
        add_row("Ordner:",             self.entry_folder,   4)
        add_row("Intervall (Min.):",   self.entry_interval, 5)
        add_row("Mail-Client-Befehl:", self.entry_client,   6)

        grid.attach(self.chk_autostart, 0, 7, 2, 1)

        hint = Gtk.Label(xalign=0)
        hint.set_markup(
            '<span size="small" foreground="#888888">'
            "🔒 Passwort und Mail-Client-Pfad werden sicher im GNOME Keyring gespeichert —\n"
            "    niemals im Klartext auf der Festplatte."
            "</span>"
        )
        grid.attach(hint, 0, 8, 2, 1)
        self.show_all()

    def get_values(self) -> dict | None:
        server    = self.entry_server.get_text().strip()
        port_str  = self.entry_port.get_text().strip()
        username  = self.entry_user.get_text().strip()
        password  = self.entry_password.get_text()
        folder    = self.entry_folder.get_text().strip()
        interval  = self.entry_interval.get_text().strip()
        client    = self.entry_client.get_text().strip()
        autostart = self.chk_autostart.get_active()

        errors = []

        if not validate_hostname(server):
            errors.append(f"Ungültiger IMAP-Server: '{server}'")

        try:
            port = validate_port(port_str)
        except ValueError as e:
            errors.append(str(e))
            port = 993

        if not validate_folder(folder):
            errors.append(f"Ungültiger Ordnername: '{folder}'")

        try:
            interval_int = int(interval)
            if not (1 <= interval_int <= 1440):
                raise ValueError()
        except ValueError:
            errors.append("Intervall muss eine Zahl zwischen 1 und 1440 sein.")
            interval_int = 5

        if client:
            try:
                validate_mail_client(client)
            except ValueError as e:
                errors.append(str(e))

        if errors:
            show_error_dialog("\n\n".join(errors))
            return None

        return {
            "server":         server,
            "port":           str(port),
            "username":       username,
            "password":       password,
            "folder":         folder,
            "check_interval": str(interval_int),
            "mail_client":    client,
            "autostart":      str(autostart).lower(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# HAUPT-APPLIKATION
# ══════════════════════════════════════════════════════════════════════════════

class MailNotifier:
    def __init__(self):
        ensure_config_dir()
        self.config          = load_config()
        self.new_mail        = False
        self._timer_id       = None
        self._last_known_uid = None   # None = noch kein Check gelaufen
                                      # 0    = Postfach war beim ersten Check leer

        self.tray = Gtk.StatusIcon()
        self.tray.set_from_file(ICON_GREY)
        self.tray.set_tooltip_text("Mailnotifier — Keine neuen Mails")
        self.tray.connect("activate",   self._on_left_click)
        self.tray.connect("popup-menu", self._on_right_click)

        self._schedule_check()
        log.info("Mailnotifier gestartet.")

    # ── Icon ──────────────────────────────────────────────────────────────

    def _set_icon(self, has_mail: bool) -> None:
        GLib.idle_add(self._update_icon_idle, has_mail)

    def _update_icon_idle(self, has_mail: bool) -> bool:
        if has_mail:
            self.tray.set_from_file(ICON_BLUE)
            self.tray.set_tooltip_text("Mailnotifier — Neue Mails vorhanden! 📬")
        else:
            self.tray.set_from_file(ICON_GREY)
            self.tray.set_tooltip_text("Mailnotifier — Keine neuen Mails")
        return False

    # ── Desktop-Benachrichtigung ──────────────────────────────────────────

    def _show_notification(self, new_count: int, total_unseen: int) -> None:
        GLib.idle_add(self._notify_idle, new_count, total_unseen)

    def _notify_idle(self, new_count: int, total_unseen: int) -> bool:
        summary = (
            "📬 1 neue Mail"
            if new_count == 1
            else f"📬 {new_count} neue Mails"
        )
        body = f"Insgesamt {total_unseen} ungelesene Mail(s) im Posteingang."
        try:
            subprocess.run(
                [
                    "notify-send",
                    "--urgency=normal",
                    "--expire-time=8000",
                    f"--icon={ICON_BLUE}",
                    "--app-name=Mailnotifier",
                    summary,
                    body,
                ],
                shell=False,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"Popup: {new_count} neue, {total_unseen} ungelesen gesamt.")
        except FileNotFoundError:
            log.warning("notify-send nicht gefunden.")
        except Exception as e:
            log.error(f"Benachrichtigungs-Fehler: {e}")
        return False

    # ── Mail-Check ────────────────────────────────────────────────────────

    def _schedule_check(self) -> None:
        try:
            interval_min = int(self.config["IMAP"].get("check_interval", "5"))
            interval_min = max(1, min(interval_min, 1440))
        except ValueError:
            interval_min = 5

        interval_ms = interval_min * 60 * 1000
        threading.Thread(target=self._do_check, daemon=True).start()
        self._timer_id = GLib.timeout_add(interval_ms, self._check_and_reschedule)

    def _check_and_reschedule(self) -> bool:
        threading.Thread(target=self._do_check, daemon=True).start()
        return True

    def _do_check(self) -> None:
        result = check_mail(self.config)

        if result is None:
            # Verbindungsfehler → Zustand nicht ändern
            return

        unseen_uids  = result["unseen_uids"]
        total_unseen = result["total_unseen"]
        max_uid      = result["max_uid"]

        # ── Erster Check nach Programmstart ───────────────────────────────
        if self._last_known_uid is None:
            # Baseline setzen — KEIN Popup, KEIN Icon-Wechsel
            # (Mails die schon vorher da waren zählen nicht als "neu")
            self._last_known_uid = max_uid
            log.info(
                f"Erster Check — Baseline gesetzt: UID={max_uid}, "
                f"{total_unseen} ungelesene Mail(s) ignoriert."
            )
            return

        # ── Folge-Checks: nur UIDs > last_known_uid sind wirklich neu ─────
        new_uids  = [uid for uid in unseen_uids if uid > self._last_known_uid]
        new_count = len(new_uids)

        if new_count > 0:
            # Neue Mails seit letztem Check!
            self._last_known_uid = max_uid   # Baseline auf neueste Mail heben
            self.new_mail        = True
            self._set_icon(True)
            self._show_notification(new_count, total_unseen)
            log.info(
                f"{new_count} neue Mail(s) gefunden "
                f"(UIDs: {new_uids}). Neue Baseline: {max_uid}."
            )
        else:
            # Keine neuen Mails — aber prüfen ob Postfach geleert wurde
            if total_unseen == 0 and self.new_mail:
                self.new_mail = False
                self._set_icon(False)
                log.info("Postfach geleert — Icon zurückgesetzt.")
            else:
                log.info(
                    f"Keine neuen Mails. "
                    f"Ungelesen gesamt: {total_unseen}, Baseline UID: {self._last_known_uid}."
                )

    # ── Klick-Handler ─────────────────────────────────────────────────────

    def _on_left_click(self, icon) -> None:
        """
        Mail-Client öffnen + Icon zurücksetzen.
        last_known_uid wird NICHT zurückgesetzt — der Nutzer
        hat die Mails gesehen, aber im IMAP könnten sie noch
        als UNSEEN markiert sein bis er sie wirklich öffnet.
        """
        open_mail_client()
        self.new_mail = False
        self._set_icon(False)
        log.info("Mail-Client geöffnet — Icon auf grau gesetzt.")

    def _on_right_click(self, icon, button, activate_time) -> None:
        menu = Gtk.Menu()

        item_settings = Gtk.MenuItem(label="⚙  Einstellungen")
        item_settings.connect("activate", self._open_settings)
        menu.append(item_settings)

        item_check = Gtk.MenuItem(label="🔄  Jetzt prüfen")
        item_check.connect(
            "activate",
            lambda _: threading.Thread(target=self._do_check, daemon=True).start()
        )
        menu.append(item_check)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="✕  Beenden")
        item_quit.connect("activate", self._quit)
        menu.append(item_quit)

        menu.show_all()
        menu.popup(None, None, None, None, button, activate_time)

    # ── Einstellungen ─────────────────────────────────────────────────────

    def _open_settings(self, _) -> None:
        dialog   = SettingsDialog(None, self.config)
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            values = dialog.get_values()
            if values is not None:
                password    = values.pop("password",    "")
                mail_client = values.pop("mail_client", "")

                if password:
                    try:
                        PasswordManager.save(
                            KEYRING_KEY_PASSWORD, password, "IMAP Password"
                        )
                    except Exception as e:
                        show_error_dialog(
                            f"Passwort konnte nicht gespeichert werden:\n{e}"
                        )
                if mail_client:
                    try:
                        PasswordManager.save(
                            KEYRING_KEY_MAILCLIENT, mail_client, "Mail Client Command"
                        )
                    except Exception as e:
                        show_error_dialog(
                            f"Mail-Client-Pfad konnte nicht gespeichert werden:\n{e}"
                        )

                for key, val in values.items():
                    self.config["IMAP"][key] = val

                for sensitive in ("password", "mail_client"):
                    if sensitive in self.config["IMAP"]:
                        del self.config["IMAP"][sensitive]

                save_config(self.config)
                set_autostart(self.config["IMAP"].getboolean("autostart", True))

                if self._timer_id is not None:
                    GLib.source_remove(self._timer_id)

                # Baseline zurücksetzen damit der nächste
                # Check eine frische Baseline setzt
                self._last_known_uid = None
                self._schedule_check()

        dialog.destroy()

    def _quit(self, _) -> None:
        log.info("Mailnotifier beendet.")
        Gtk.main_quit()

    def run(self) -> None:
        Gtk.main()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not SECRETSTORAGE_AVAILABLE:
        log.warning(
            "WARNUNG: secretstorage nicht installiert!\n"
            "Bitte installieren: sudo apt install python3-secretstorage"
        )
    app = MailNotifier()
    app.run()
