#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MailNotifier – Linux Mint Taskleisten-Mail-Checker
Version: 2.0  (StatusIcon – echte Links/Rechtsklick-Trennung)
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Notify', '0.7')

from gi.repository import Gtk, GdkPixbuf, Notify, GLib
import imaplib
import threading
import configparser
import os
import subprocess
import logging
from pathlib import Path

# ──────────────────────────────────────────────
# Pfade & Konstanten
# ──────────────────────────────────────────────
APP_ID        = "mailnotifier"
APP_NAME      = "Mail Notifier"
BASE_DIR      = Path.home() / ".local/share/mailnotifier"
CONFIG_FILE   = Path.home() / ".config/mailnotifier.ini"
AUTOSTART_DIR = Path.home() / ".config/autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "mailnotifier.desktop"
ICON_GREY     = str(BASE_DIR / "icon_grey.svg")
ICON_BLUE     = str(BASE_DIR / "icon_blue.svg")
SCRIPT_PATH   = str(BASE_DIR / "mailnotifier.py")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(APP_NAME)

# ──────────────────────────────────────────────
# Konfiguration
# ──────────────────────────────────────────────
class Config:
    DEFAULTS = {
        "imap_server":   "",
        "imap_port":     "993",
        "imap_user":     "",
        "imap_password": "",
        "imap_ssl":      "true",
        "imap_folder":   "INBOX",
        "interval":      "5",
        "mail_client":   "",
        "autostart":     "true",
    }

    def __init__(self):
        self.cfg = configparser.ConfigParser()
        self.cfg["settings"] = self.DEFAULTS.copy()
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            self.cfg.read(CONFIG_FILE)

    def save(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            self.cfg.write(f)

    def get(self, key):
        return self.cfg.get("settings", key, fallback=self.DEFAULTS.get(key, ""))

    def set(self, key, value):
        self.cfg.set("settings", key, str(value))

    @property
    def autostart_enabled(self):
        return self.get("autostart").lower() == "true"

    @property
    def interval_seconds(self):
        try:
            return max(1, int(self.get("interval"))) * 60
        except ValueError:
            return 300

# ──────────────────────────────────────────────
# Autostart
# ──────────────────────────────────────────────
DESKTOP_CONTENT = """\
[Desktop Entry]
Type=Application
Name=Mail Notifier
Comment=IMAP Mail Checker
Exec=python3 {script}
Icon={icon}
Terminal=false
Categories=Utility;Network;Email;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""

def enable_autostart():
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        DESKTOP_CONTENT.format(script=SCRIPT_PATH, icon=ICON_GREY)
    )
    log.info("Autostart aktiviert")

def disable_autostart():
    if AUTOSTART_FILE.exists():
        AUTOSTART_FILE.unlink()
    log.info("Autostart deaktiviert")

# ──────────────────────────────────────────────
# IMAP-Checker
# ──────────────────────────────────────────────
class IMAPChecker:
    def __init__(self, config: Config):
        self.config       = config
        self._last_uids: set = set()
        self._initialized = False

    def check(self) -> int:
        server   = self.config.get("imap_server")
        port     = int(self.config.get("imap_port"))
        user     = self.config.get("imap_user")
        password = self.config.get("imap_password")
        use_ssl  = self.config.get("imap_ssl").lower() == "true"
        folder   = self.config.get("imap_folder") or "INBOX"

        if not server or not user or not password:
            log.warning("IMAP-Zugangsdaten unvollständig")
            return 0

        try:
            conn = (imaplib.IMAP4_SSL(server, port)
                    if use_ssl else imaplib.IMAP4(server, port))
            conn.login(user, password)
            conn.select(folder, readonly=True)
            status, data = conn.uid("search", None, "ALL")
            current_uids = set(data[0].split()) if data[0] else set()
            conn.logout()

            if not self._initialized:
                self._last_uids   = current_uids
                self._initialized = True
                log.info(f"Baseline gesetzt: {len(current_uids)} Mails")
                return 0

            new_uids        = current_uids - self._last_uids
            self._last_uids = current_uids
            count = len(new_uids)
            if count:
                log.info(f"{count} neue Mail(s)")
            return count

        except imaplib.IMAP4.error as e:
            log.error(f"IMAP-Fehler: {e}")
            return 0
        except Exception as e:
            log.error(f"Verbindungsfehler: {e}")
            return 0

    def reset_baseline(self):
        self._initialized = False

# ──────────────────────────────────────────────
# Einstellungsdialog
# ──────────────────────────────────────────────
class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent_app):
        super().__init__(title="Mail Notifier – Einstellungen", flags=0)
        self.app    = parent_app
        self.config = parent_app.config
        self.set_default_size(440, 500)
        self.set_border_width(10)
        self.set_resizable(False)

        self.add_button("Abbrechen", Gtk.ResponseType.CANCEL)
        ok_btn = self.add_button("Speichern", Gtk.ResponseType.OK)
        ok_btn.get_style_context().add_class("suggested-action")

        box = self.get_content_area()
        box.set_spacing(6)
        notebook = Gtk.Notebook()
        box.pack_start(notebook, True, True, 0)

        # ── Tab IMAP ──────────────────────────
        g = Gtk.Grid(column_spacing=12, row_spacing=10, border_width=16)
        notebook.append_page(g, Gtk.Label(label="📧  IMAP"))

        def lbl(text):
            l = Gtk.Label(label=text, xalign=1.0)
            l.get_style_context().add_class("dim-label")
            return l

        self.e_server = Gtk.Entry(
            text=self.config.get("imap_server"),
            placeholder_text="mail.example.com")
        self.e_port   = Gtk.Entry(
            text=self.config.get("imap_port"),
            placeholder_text="993")
        self.e_user   = Gtk.Entry(
            text=self.config.get("imap_user"),
            placeholder_text="benutzer@example.com")
        self.e_pass   = Gtk.Entry(
            text=self.config.get("imap_password"),
            visibility=False)
        self.e_pass.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.e_folder = Gtk.Entry(
            text=self.config.get("imap_folder"),
            placeholder_text="INBOX")
        self.chk_ssl  = Gtk.CheckButton(label="SSL/TLS verwenden")
        self.chk_ssl.set_active(
            self.config.get("imap_ssl").lower() == "true")

        for i, (label_text, widget) in enumerate([
            ("Server:",   self.e_server),
            ("Port:",     self.e_port),
            ("Benutzer:", self.e_user),
            ("Passwort:", self.e_pass),
            ("Ordner:",   self.e_folder),
        ]):
            g.attach(lbl(label_text), 0, i, 1, 1)
            g.attach(widget,          1, i, 2, 1)
            widget.set_hexpand(True)

        g.attach(self.chk_ssl, 1, 5, 2, 1)

        btn_test = Gtk.Button(label="🔌  Verbindung testen")
        btn_test.connect("clicked", self._on_test_connection)
        g.attach(btn_test, 1, 6, 2, 1)

        # ── Tab Allgemein ─────────────────────
        g2 = Gtk.Grid(column_spacing=12, row_spacing=10, border_width=16)
        notebook.append_page(g2, Gtk.Label(label="⚙  Allgemein"))

        self.e_interval = Gtk.SpinButton.new_with_range(1, 120, 1)
        try:
            self.e_interval.set_value(int(self.config.get("interval")))
        except ValueError:
            self.e_interval.set_value(5)

        self.e_client  = Gtk.Entry(
            text=self.config.get("mail_client"),
            placeholder_text="/usr/bin/thunderbird")
        self.e_client.set_hexpand(True)
        btn_browse = Gtk.Button(label="📂")
        btn_browse.connect("clicked", self._on_browse_client)

        self.chk_autostart = Gtk.CheckButton(label="Autostart beim Login aktivieren")
        self.chk_autostart.set_active(self.config.autostart_enabled)

        g2.attach(lbl("Intervall (Min):"), 0, 0, 1, 1)
        g2.attach(self.e_interval,         1, 0, 2, 1)
        g2.attach(lbl("Mail-Programm:"),   0, 1, 1, 1)
        g2.attach(self.e_client,           1, 1, 1, 1)
        g2.attach(btn_browse,              2, 1, 1, 1)
        g2.attach(self.chk_autostart,      1, 2, 2, 1)

        self.show_all()

    def _on_browse_client(self, _):
        dlg = Gtk.FileChooserDialog(
            title="Mail-Programm auswählen",
            parent=self,
            action=Gtk.FileChooserAction.OPEN
        )
        dlg.add_buttons("Abbrechen", Gtk.ResponseType.CANCEL,
                         "Auswählen",  Gtk.ResponseType.OK)
        dlg.set_current_folder("/usr/bin")
        if dlg.run() == Gtk.ResponseType.OK:
            self.e_client.set_text(dlg.get_filename())
        dlg.destroy()

    def _on_test_connection(self, _):
        server   = self.e_server.get_text().strip()
        port     = int(self.e_port.get_text().strip() or "993")
        user     = self.e_user.get_text().strip()
        password = self.e_pass.get_text()
        use_ssl  = self.chk_ssl.get_active()

        if not server or not user or not password:
            self._msg("Fehler", "Bitte alle IMAP-Felder ausfüllen.",
                      Gtk.MessageType.ERROR)
            return

        def do_test():
            try:
                conn = (imaplib.IMAP4_SSL(server, port)
                        if use_ssl else imaplib.IMAP4(server, port))
                conn.login(user, password)
                conn.select("INBOX", readonly=True)
                _, data = conn.uid("search", None, "ALL")
                count = len(data[0].split()) if data[0] else 0
                conn.logout()
                GLib.idle_add(self._msg,
                    "Verbindung erfolgreich",
                    f"✅ Verbunden!\n{count} Mails in INBOX.",
                    Gtk.MessageType.INFO)
            except Exception as e:
                GLib.idle_add(self._msg,
                    "Verbindungsfehler",
                    f"❌ Fehler:\n{e}",
                    Gtk.MessageType.ERROR)

        threading.Thread(target=do_test, daemon=True).start()

    def _msg(self, title, body, mtype=Gtk.MessageType.INFO):
        d = Gtk.MessageDialog(transient_for=self, flags=0,
                              message_type=mtype,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(body)
        d.run(); d.destroy()

    def save_to_config(self):
        self.config.set("imap_server",   self.e_server.get_text().strip())
        self.config.set("imap_port",     self.e_port.get_text().strip())
        self.config.set("imap_user",     self.e_user.get_text().strip())
        self.config.set("imap_password", self.e_pass.get_text())
        self.config.set("imap_ssl",      str(self.chk_ssl.get_active()).lower())
        self.config.set("imap_folder",   self.e_folder.get_text().strip() or "INBOX")
        self.config.set("interval",      str(int(self.e_interval.get_value())))
        self.config.set("mail_client",   self.e_client.get_text().strip())
        self.config.set("autostart",     str(self.chk_autostart.get_active()).lower())
        self.config.save()

        if self.chk_autostart.get_active():
            enable_autostart()
        else:
            disable_autostart()

# ──────────────────────────────────────────────
# Hauptanwendung  ← KERN DER ÄNDERUNG
# ──────────────────────────────────────────────
class MailNotifierApp:
    def __init__(self):
        self.config       = Config()
        self.checker      = IMAPChecker(self.config)
        self.has_new_mail = False
        self._timer_id    = None

        Notify.init(APP_NAME)

        # ── Gtk.StatusIcon statt AppIndicator3 ──────────────────────────────
        # StatusIcon trennt Linksklick (activate) und Rechtsklick (popup-menu)
        # sauber auf Signalebene – perfekt für Linux Mint / Cinnamon.
        # ────────────────────────────────────────────────────────────────────
        self.tray = Gtk.StatusIcon()
        self.tray.set_from_file(ICON_GREY)
        self.tray.set_tooltip_text(APP_NAME)
        self.tray.set_visible(True)

        # LINKSKLICK  → Mail-Client öffnen + Icon zurücksetzen
        self.tray.connect("activate", self._on_left_click)

        # RECHTSKLICK → Kontextmenü anzeigen
        self.tray.connect("popup-menu", self._on_right_click)

        self._schedule_check()
        log.info(f"{APP_NAME} gestartet – Intervall: "
                 f"{self.config.get('interval')} Min.")

    # ── Icon-Zustand ─────────────────────────────────────────────────────
    def _set_icon_blue(self):
        self.tray.set_from_file(ICON_BLUE)
        self.tray.set_tooltip_text(f"{APP_NAME} – Neue Mail(s)!")

    def _set_icon_grey(self):
        self.tray.set_from_file(ICON_GREY)
        self.tray.set_tooltip_text(APP_NAME)

    # ── Linksklick ───────────────────────────────────────────────────────
    def _on_left_click(self, _icon):
        """Linksklick: Mail-Programm direkt starten, Icon → grau."""
        client = self.config.get("mail_client")
        if client:
            try:
                subprocess.Popen(client, shell=True)
                log.info(f"Mail-Client gestartet: {client}")
            except Exception as e:
                log.error(f"Fehler beim Starten: {e}")
                self._notify("Fehler", f"Konnte '{client}' nicht starten:\n{e}")
        else:
            self._notify(
                "Kein Mail-Programm konfiguriert",
                "Bitte in den Einstellungen (Rechtsklick) einen Pfad angeben."
            )

        # Icon immer zurücksetzen, egal ob Client gefunden oder nicht
        if self.has_new_mail:
            self.has_new_mail = False
            self._set_icon_grey()

    # ── Rechtsklick ──────────────────────────────────────────────────────
    def _on_right_click(self, _icon, button, activate_time):
        """Rechtsklick: Kontextmenü anzeigen."""
        menu = Gtk.Menu()

        # Kopfzeile (nicht klickbar) – zeigt Status
        status_label = (
            "📬  Neue Mails vorhanden" if self.has_new_mail
            else "📭  Keine neuen Mails"
        )
        item_status = Gtk.MenuItem(label=status_label)
        item_status.set_sensitive(False)          # nur zur Info
        menu.append(item_status)

        menu.append(Gtk.SeparatorMenuItem())

        item_check = Gtk.MenuItem(label="🔄  Jetzt prüfen")
        item_check.connect("activate", lambda _: self._trigger_check())
        menu.append(item_check)

        menu.append(Gtk.SeparatorMenuItem())

        item_settings = Gtk.MenuItem(label="⚙  Einstellungen…")
        item_settings.connect("activate", self._on_settings)
        menu.append(item_settings)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="✕  Beenden")
        item_quit.connect("activate", self._on_quit)
        menu.append(item_quit)

        menu.show_all()
        menu.popup(None, None,
                   Gtk.StatusIcon.position_menu,  # Menü am Icon ausrichten
                   self.tray,
                   button, activate_time)

    # ── Einstellungen ────────────────────────────────────────────────────
    def _on_settings(self, _):
        dlg = SettingsDialog(self)
        if dlg.run() == Gtk.ResponseType.OK:
            dlg.save_to_config()
            self._schedule_check(restart=True)
            log.info("Einstellungen gespeichert & Timer neu gestartet")
        dlg.destroy()

    # ── Beenden ──────────────────────────────────────────────────────────
    def _on_quit(self, _):
        log.info("Beende Anwendung")
        Notify.uninit()
        Gtk.main_quit()

    # ── Check-Timer ──────────────────────────────────────────────────────
    def _schedule_check(self, restart=False):
        if restart and self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

        # Sofort einmal prüfen
        threading.Thread(target=self._do_check, daemon=True).start()

        # Dann im eingestellten Intervall wiederholen
        interval_ms    = self.config.interval_seconds * 1000
        self._timer_id = GLib.timeout_add(interval_ms, self._timer_callback)

    def _timer_callback(self):
        threading.Thread(target=self._do_check, daemon=True).start()
        return True  # True = Timeout wiederholen

    def _trigger_check(self):
        """Manuell aus dem Menü heraus anstoßen."""
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self):
        count = self.checker.check()
        if count > 0:
            GLib.idle_add(self._on_new_mail, count)

    def _on_new_mail(self, count):
        self.has_new_mail = True
        self._set_icon_blue()
        text = f"{count} neue Mail" if count == 1 else f"{count} neue Mails"
        self._notify("📬 Neue Mail(s)!", text)

    def _notify(self, title, body):
        try:
            n = Notify.Notification.new(title, body, ICON_BLUE)
            n.show()
        except Exception as e:
            log.warning(f"Desktop-Benachrichtigung fehlgeschlagen: {e}")

    def run(self):
        Gtk.main()

# ──────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    app = MailNotifierApp()
    app.run()
