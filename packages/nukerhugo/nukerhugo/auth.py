"""Key setup shared by every Nukerhugo tool: free-key signup, paste a key, custom endpoint."""
from __future__ import annotations

import getpass
import os

from .api import ApiError, Client, find_value
from .config import DEFAULT_BASE_URL, save


def extract_key(resp) -> str | None:
    val = find_value(resp, ("api_key", "key", "apikey", "token", "secret"))
    return str(val) if val else None


def verify(cfg, ui) -> bool:
    """Check that the key works. Returns False only on a definite auth failure."""
    client = Client(cfg)
    try:
        client.usage() if cfg.base_url == DEFAULT_BASE_URL else client.list_models()
        return True
    except ApiError as e:
        if e.kind == "auth":
            ui.error(e.message)
            return False
        ui.info(f"  (could not verify the connection: {e.message.splitlines()[0]})")
        return True


def login(cfg, ui, ask_model: bool = False, section: str = "code") -> bool:
    """Interactive setup. Saves base_url and api_key (and the model, if asked for)."""
    ui.out()
    ui.out(ui.c("  Nukerhugo login", "bold", "cyan"))
    ui.out(ui.c("  You need an API key to talk to the model.", "dim"))
    ui.out()
    ui.out("  1  Get a free Nukerhugo key (100,000 tokens/day, no credit card)")
    ui.out("  2  I already have a key")
    ui.out("  3  Use a different endpoint (local model or another provider)")
    ui.out()
    try:
        choice = ui.ask("  Choose [1-3]: ")
        if choice == "1":
            ok, keys = _signup(cfg, ui), ("base_url", "api_key")
        elif choice == "2":
            ok, keys = _paste_key(cfg, ui), ("base_url", "api_key")
        elif choice == "3":
            ok = _custom_endpoint(cfg, ui, ask_model)
            keys = ("base_url", "api_key") + (("model",) if ask_model else ())
        else:
            ui.error("Pick 1, 2 or 3.")
            return False
    except (KeyboardInterrupt, EOFError):
        ui.out()
        return False
    if ok:
        path = save(cfg, section=section, keys=keys)
        where = "stored in your user profile" if os.name == "nt" else "owner-only permissions"
        ui.info(f"  Saved to {path} ({where}).")
    return ok


def _signup(cfg, ui) -> bool:
    ui.out()
    ui.info("  Your email is sent to Nukerhugo once; only a hash of it is stored.")
    ui.info("  One free key per address. The key is shown once, so it is saved for you.")
    email = ui.ask("  Email address: ").strip().strip("'\"")
    if not _plausible_email(email):
        ui.error("That does not look like an email address.")
        return False
    ui.spinner_start("Requesting key")
    try:
        resp = Client(cfg).signup(email)
    except ApiError as e:
        ui.spinner_stop()
        if e.kind == "conflict" or e.status in (400, 422) or "exist" in e.message.lower():
            ui.error(f"{e.message}\n  If you lost your key, email hello@nukerhugo.nl.")
        else:
            ui.error(f"Signup failed: {e.message}")
        return False
    ui.spinner_stop()
    key = extract_key(resp)
    if not key:
        ui.warn("Signup answered, but I could not find a key in the reply:")
        ui.out(str(resp)[:400])
        return _paste_key(cfg, ui)
    cfg.api_key, cfg.key_source = key, "file"
    ui.out()
    ui.out("  " + ui.c("Your key (shown only once): ", "bold") + ui.c(key, "green", "bold"))
    return True


def _plausible_email(email: str) -> bool:
    if any(c in email for c in " \t'\"<>,;"):
        return False
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


def _paste_key(cfg, ui) -> bool:
    for _ in range(3):
        key = getpass.getpass("  Paste your API key (hidden): ").strip()
        if not key:
            return False
        cfg.api_key, cfg.key_source = key, "file"
        if verify(cfg, ui):
            return True
        ui.info("  Try again, or press Enter to cancel.")
    return False


def _custom_endpoint(cfg, ui, ask_model: bool) -> bool:
    url = ui.ask("  Base URL (e.g. http://localhost:11434/v1): ").rstrip("/")
    if not url.startswith(("http://", "https://")):
        ui.error("The base URL must start with http:// or https://")
        return False
    key = getpass.getpass("  API key (hidden, Enter for none): ").strip()
    if ask_model:
        model = ui.ask("  Model name: ")
        if not model:
            ui.error("A model name is required for a custom endpoint.")
            return False
        cfg.model = model
    cfg.base_url, cfg.api_key = url, key
    cfg.key_source = "file" if key else ""
    return verify(cfg, ui)
