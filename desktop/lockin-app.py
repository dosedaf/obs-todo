#!/usr/bin/python3
"""Lock In — standalone desktop window for the dashboard.

Native GTK4 + WebKit shell around http://localhost:8787/manage so it behaves
as a singular app on GNOME: own window, own taskbar icon, single instance.
Run with the SYSTEM python (/usr/bin/python3) — Homebrew python lacks gi.
"""
import socket
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, GLib, Gio, Gtk, WebKit

APP_ID = "LockIn"
BASE = "http://localhost:8787"
URL = BASE + "/manage"
SERVICE = "obs-todo"


def server_up() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 8787), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


class LockInWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Lock In")
        self.set_default_size(1100, 800)

        self.webview = WebKit.WebView()
        self.webview.connect("decide-policy", self.on_policy)
        self.webview.connect("create", self.on_create)

        # fallback page when the service is down, with a retry button
        self.fallback = self._fallback_page()
        overlay = Gtk.Overlay()
        overlay.set_child(self.webview)
        overlay.add_overlay(self.fallback)

        self.set_content(overlay)
        self.install_action("win.reload", None, lambda w, a, p: self.reload())
        self.add_shortcut(Gtk.Shortcut.new(
            Gtk.KeyTrigger.new("r", Gtk.ModifierType.CONTROL_MASK),
            Gtk.NamedAction.new("win.reload")))

        self.reload()

    def _fallback_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        title = Gtk.Label()
        title.set_markup("<big><b>lock in service isn't running</b></big>")
        hint = Gtk.Label()
        hint.set_markup(f'<small>start it with <tt>systemctl --user start {SERVICE}</tt></small>')
        btn = Gtk.Button(label="start it for me")
        btn.connect("clicked", self._on_start)
        box.append(title)
        box.append(hint)
        box.append(btn)
        box.add_css_class("osd")
        return box

    def _on_start(self, _btn):
        try:
            GLib.spawn_command_line_async(f"systemctl --user start {SERVICE}")
        except GLib.Error:
            pass
        # give the service a beat, then retry
        GLib.timeout_add(700, self._retry_when_up)

    def _retry_when_up(self):
        if server_up():
            self.fallback.set_visible(False)
            self.reload()
            return False
        return True  # keep polling

    def reload(self):
        if server_up():
            self.fallback.set_visible(False)
            self.webview.load_uri(URL)
        else:
            self.fallback.set_visible(True)

    def on_policy(self, _view, decision, dtype):
        """Keep localhost in-app; hand external links to the default browser."""
        if dtype != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        try:
            host = GLib.Uri.parse(uri, GLib.UriFlags.NONE).get_host()
        except Exception:
            host = None
        if host not in ("localhost", "127.0.0.1", None):
            Gtk.show_uri(None, uri, 0)
            decision.ignore()
            return True
        return False

    def on_create(self, _view, nav):
        """window.open() (e.g. the PNG card exports) -> default browser."""
        uri = nav.get_request().get_uri()
        if uri:
            Gtk.show_uri(None, uri, 0)
        return None


class LockInApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)  # unique by default -> singular app
        self.win = None

    def do_activate(self):
        if self.win is None:
            self.win = LockInWindow(application=self)
        self.win.present()


if __name__ == "__main__":
    sys.exit(LockInApp().run(sys.argv))
