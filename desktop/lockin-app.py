#!/usr/bin/python3
"""Lock In — standalone desktop window for the dashboard.

Native GTK4 + WebKit shell around http://localhost:8787/manage so it behaves
as a singular app on GNOME: own window, header bar with minimize/maximize/
close, hide-to-background, zoom controls, remembered geometry.
Run with the SYSTEM python (/usr/bin/python3) — Homebrew python lacks gi.
"""
import json
import socket
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, WebKit

APP_ID = "org.lockin.app"
BASE = "http://localhost:8787"
URL = BASE + "/manage"
SERVICE = "obs-todo"
CONFIG = Path.home() / ".config" / "lockin-app.json"

# 1998-flavored chrome for the header bar
APP_CSS = b"""
headerbar {
  background: linear-gradient(180deg, #2a1a08 0%, #180d00 100%);
  border-bottom: 2px solid #6a4a10;
  box-shadow: none;
  color: #d8a248;
}
headerbar:backdrop {
  background: #180d00;
  color: #a87830;
}
headerbar .title, headerbar .subtitle {
  color: #d8a248;
  font-weight: 700;
  letter-spacing: 2px;
}
headerbar:backdrop .title { color: #a87830; }
headerbar button:not(.windowcontrol) {
  background: none; border: none; box-shadow: none;
  color: #d8a248; border-radius: 0;
}
headerbar button:not(.windowcontrol):hover { background: rgba(216, 162, 72, 0.15); }
headerbar windowcontrols > button { background: none; box-shadow: none; }
headerbar windowcontrols > button image { color: #d8a248; }
headerbar windowcontrols > button:hover image { color: #f0d0a0; }
"""


def server_up() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 8787), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg))
    except Exception:
        pass


class LockInWindow(Adw.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app)
        self.cfg = cfg
        self.set_title("Lock In")
        self.set_default_size(cfg.get("w", 1280), cfg.get("h", 860))
        if cfg.get("maximized"):
            self.maximize()

        self.webview = WebKit.WebView()
        self.webview.connect("decide-policy", self.on_policy)
        self.webview.connect("create", self.on_create)
        self.zoom = float(cfg.get("zoom", 1.3))
        self.webview.set_zoom_level(self.zoom)

        # fallback page when the service is down, with a retry button
        self.fallback = self._fallback_page()
        overlay = Gtk.Overlay()
        overlay.set_child(self.webview)
        overlay.add_overlay(self.fallback)

        # header bar: 1998 bronze chrome + real window controls
        # (Adw.HeaderBar shows minimize/maximize/close by default)
        hb = Adw.HeaderBar()
        hb.set_title_widget(self._title_label())
        bg_btn = Gtk.Button.new_from_icon_name("view-conceal-symbolic")
        bg_btn.set_tooltip_text("hide to background (reopen from Activities)")
        bg_btn.connect("clicked", self._on_background)
        hb.pack_start(bg_btn)

        tv = Adw.ToolbarView()
        tv.add_top_bar(hb)
        tv.set_content(overlay)
        self.set_content(tv)

        # window actions
        self.install_action("win.reload", None, lambda w, a, p: self.reload())
        self.install_action("win.zoom-in", None, lambda w, a, p: self.zoom_step(1.1))
        self.install_action("win.zoom-out", None, lambda w, a, p: self.zoom_step(1 / 1.1))
        self.install_action("win.zoom-100", None, self._zoom_reset)
        self.install_action("win.background", None, lambda w, a, p: self._on_background())
        for combo, action in [
            ("<Control>r", "win.reload"),
            ("<Control>plus", "win.zoom-in"),
            ("<Control>equal", "win.zoom-in"),
            ("<Control>minus", "win.zoom-out"),
            ("<Control>0", "win.zoom-100"),
            ("F11", "window.toggle-maximized"),
            ("<Control>w", "window.background"),
        ]:
            self.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(combo),
                Gtk.NamedAction.new(action)))

        self.connect("close-request", self._on_close_request)
        self.reload()

    # ---------- chrome helpers
    def _title_label(self) -> Gtk.Label:
        lbl = Gtk.Label(label="LOCK IN")
        lbl.add_css_class("title")
        return lbl

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

    # ---------- behaviors
    def _save_geometry(self) -> None:
        self.cfg.update({
            "w": self.get_width(), "h": self.get_height(),
            "maximized": self.is_maximized(), "zoom": round(self.zoom, 3),
        })
        save_config(self.cfg)

    def _on_close_request(self, _w):
        self._save_geometry()
        return False  # let the window close

    def _on_background(self, *_a):
        """Hide to background: app keeps running, relaunch from Activities restores."""
        self._save_geometry()
        self.hide()
        app = self.get_application()
        if app is not None and not getattr(app, "_told_background", False):
            app._told_background = True
            n = Gio.Notification.new("Lock In is running in the background")
            n.set_body("open Lock In from Activities to bring it back")
            n.set_icon(Gio.ThemedIcon.new("lock-in"))
            app.send_notification("background", n)

    def _on_start(self, _btn):
        try:
            GLib.spawn_command_line_async(f"systemctl --user start {SERVICE}")
        except GLib.Error:
            pass
        GLib.timeout_add(700, self._retry_when_up)

    def _retry_when_up(self):
        if server_up():
            self.fallback.set_visible(False)
            self.reload()
            return False
        return True

    def zoom_step(self, factor):
        self.zoom = min(3.0, max(0.5, self.zoom * factor))
        self.webview.set_zoom_level(self.zoom)

    def _zoom_reset(self, *_a):
        self.zoom = 1.0
        self.webview.set_zoom_level(self.zoom)

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
        self._told_background = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(APP_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def do_activate(self):
        try:
            if self.win is None:
                self.win = LockInWindow(self, load_config())
            self.win.present()
        except Exception:
            import traceback
            traceback.print_exc()
            raise


if __name__ == "__main__":
    sys.exit(LockInApp().run(sys.argv))
