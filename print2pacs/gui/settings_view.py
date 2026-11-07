"""Settings dialog: application options + destinations address book.

Changes save immediately to the portable data folder (settings.json /
destinations.json), so they persist across runs. ``on_changed`` lets the main
window refresh its Query/Send pickers.
"""

from __future__ import annotations

from tkinter import messagebox

import customtkinter as ctk

from .. import config
from ..model import validate_aetitle
from . import theme
from .dialogs import AddDestinationDialog


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_changed=None):
        super().__init__(parent)
        self.app = parent
        self.settings = parent.settings
        self.on_changed = on_changed

        self.title("Settings")
        self.geometry("580x680")
        self.minsize(520, 560)
        self.transient(parent)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self)
        body.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 0))
        body.grid_columnconfigure(1, weight=1)
        self.body = body

        self._build_app_settings()
        self._build_destinations()

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=12, pady=10)
        bar.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bar, text="", text_color=theme.MUTED,
                                   anchor="w")
        self.status.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(bar, text="Close", width=90,
                      command=self._close).grid(row=0, column=1)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ------------------------------------------------------------- app settings
    def _build_app_settings(self):
        r = 0
        ctk.CTkLabel(self.body, text="Application",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(4, 6)); r += 1

        ctk.CTkLabel(self.body, text="My AE Title (calling AE):").grid(
            row=r, column=0, sticky="w", pady=6, padx=(4, 8))
        self.ae_var = ctk.StringVar(value=self.settings.my_aetitle)
        e = ctk.CTkEntry(self.body, textvariable=self.ae_var)
        e.grid(row=r, column=1, sticky="ew", pady=6)
        e.bind("<FocusOut>", lambda _e: self._save_app())
        r += 1

        ctk.CTkLabel(self.body, text="Appearance:").grid(
            row=r, column=0, sticky="w", pady=6, padx=(4, 8))
        self.appear_var = ctk.StringVar(value=self.settings.appearance)
        ctk.CTkOptionMenu(self.body, variable=self.appear_var,
                          values=["System", "Light", "Dark"],
                          command=self._on_appearance, width=140).grid(
            row=r, column=1, sticky="w", pady=6)
        r += 1

        ctk.CTkLabel(self.body, text="PDF import DPI:").grid(
            row=r, column=0, sticky="w", pady=6, padx=(4, 8))
        self.dpi_var = ctk.StringVar(value=str(self.settings.pdf_dpi))
        e = ctk.CTkEntry(self.body, textvariable=self.dpi_var, width=100)
        e.grid(row=r, column=1, sticky="w", pady=6)
        e.bind("<FocusOut>", lambda _e: self._save_app())
        r += 1

        ctk.CTkLabel(self.body, text="Image compression:").grid(
            row=r, column=0, sticky="w", pady=6, padx=(4, 8))
        self._comp_map = {"None": "none", "RLE Lossless": "rle"}
        self._comp_rev = {v: k for k, v in self._comp_map.items()}
        self.comp_var = ctk.StringVar(
            value=self._comp_rev.get(self.settings.image_compression, "None"))
        ctk.CTkOptionMenu(self.body, variable=self.comp_var,
                          values=list(self._comp_map), width=160,
                          command=lambda _v: self._save_app()).grid(
            row=r, column=1, sticky="w", pady=6)
        r += 1

        ctk.CTkLabel(self.body, text="Imported DICOM:").grid(
            row=r, column=0, sticky="w", pady=6, padx=(4, 8))
        self.strip_var = ctk.BooleanVar(value=self.settings.strip_private_tags)
        ctk.CTkCheckBox(
            self.body, text="Strip private/vendor tags before sending",
            variable=self.strip_var, command=self._save_app).grid(
            row=r, column=1, sticky="w", pady=6)
        r += 1

        ctk.CTkLabel(self.body, text="Series number:").grid(
            row=r, column=0, sticky="w", pady=6, padx=(4, 8))
        self.series_var = ctk.StringVar(
            value=str(self.settings.default_series_number))
        e = ctk.CTkEntry(self.body, textvariable=self.series_var, width=100)
        e.grid(row=r, column=1, sticky="w", pady=6)
        e.bind("<FocusOut>", lambda _e: self._save_app())
        r += 1

        ctk.CTkLabel(self.body, text="Debug:").grid(row=r, column=0, sticky="w",
                                                    pady=6, padx=(4, 8))
        dbg = ctk.CTkFrame(self.body, fg_color="transparent")
        dbg.grid(row=r, column=1, sticky="w", pady=6)
        self.keep_var = ctk.BooleanVar(value=self.settings.keep_sent_copy)
        ctk.CTkCheckBox(dbg, text="Keep a copy of sent DICOM",
                        variable=self.keep_var,
                        command=self._save_app).pack(side="left")
        ctk.CTkButton(dbg, text="Open folder", width=90, fg_color="gray",
                      command=self._open_jobs_folder).pack(side="left",
                                                           padx=(10, 0))
        r += 1

        from .. import __version__
        ctk.CTkLabel(self.body, text="Version:").grid(
            row=r, column=0, sticky="w", pady=6, padx=(4, 8))
        vwrap = ctk.CTkFrame(self.body, fg_color="transparent")
        vwrap.grid(row=r, column=1, sticky="w", pady=6)
        ctk.CTkLabel(vwrap, text=f"v{__version__}").pack(side="left",
                                                         padx=(0, 10))
        ctk.CTkButton(vwrap, text="Check for updates", width=150,
                      command=self._check_updates).pack(side="left")
        r += 1

        self._app_row = r

    def _check_updates(self):
        self.app.check_updates(manual=True)

    def _open_jobs_folder(self):
        import os
        from .. import paths
        folder = paths.jobs_dir()
        try:
            os.startfile(str(folder))   # noqa: S606 - open the user's own folder
        except Exception:  # noqa: BLE001
            self._flash(f"Sent-copy folder: {folder}")

    def _on_appearance(self, value):
        ctk.set_appearance_mode(value)
        self._save_app()

    def _save_app(self):
        ae = self.ae_var.get().strip()
        prob = validate_aetitle(ae)
        if prob:
            self._flash(f"My AE Title: {prob}", error=True)
            return
        self.settings.my_aetitle = ae
        self.settings.appearance = self.appear_var.get()
        if hasattr(self, "keep_var"):
            self.settings.keep_sent_copy = bool(self.keep_var.get())
        if hasattr(self, "comp_var"):
            self.settings.image_compression = self._comp_map.get(
                self.comp_var.get(), "none")
        if hasattr(self, "strip_var"):
            self.settings.strip_private_tags = bool(self.strip_var.get())
        try:
            self.settings.pdf_dpi = max(50, min(600, int(self.dpi_var.get())))
        except ValueError:
            pass
        self.dpi_var.set(str(self.settings.pdf_dpi))
        try:
            self.settings.default_series_number = int(self.series_var.get())
        except ValueError:
            self.series_var.set(str(self.settings.default_series_number))
        config.save_settings(self.settings)
        self._flash("Saved.")

    # ------------------------------------------------------------- destinations
    def _build_destinations(self):
        r = self._app_row
        ctk.CTkLabel(self.body, text="").grid(row=r, column=0); r += 1
        hdr = ctk.CTkFrame(self.body, fg_color="transparent")
        hdr.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 2))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="Destinations",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(hdr, text="＋ Add PACS…", width=110,
                      command=self._add).grid(row=0, column=1, sticky="e")
        r += 1

        self.dest_list = ctk.CTkFrame(self.body)
        self.dest_list.grid(row=r, column=0, columnspan=2, sticky="ew", pady=4)
        self.dest_list.grid_columnconfigure(0, weight=1)
        r += 1

        roles = ctk.CTkFrame(self.body, fg_color="transparent")
        roles.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        roles.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(roles, text="Default Query PACS:").grid(
            row=0, column=0, sticky="w", pady=4, padx=(4, 8))
        self.query_role = ctk.CTkOptionMenu(
            roles, values=["(none)"], command=lambda _v: self._save_roles())
        self.query_role.grid(row=0, column=1, sticky="w", pady=4)
        ctk.CTkLabel(roles, text="Default Send PACS:").grid(
            row=1, column=0, sticky="w", pady=4, padx=(4, 8))
        self.send_role = ctk.CTkOptionMenu(
            roles, values=["(none)"], command=lambda _v: self._save_roles())
        self.send_role.grid(row=1, column=1, sticky="w", pady=4)

        self._refresh_dest_list()

    def _refresh_dest_list(self):
        for w in self.dest_list.winfo_children():
            w.destroy()
        dests = self.app.destinations
        if not dests:
            ctk.CTkLabel(self.dest_list, text="No destinations yet. Add one.",
                         text_color=theme.MUTED).grid(row=0, column=0,
                                                      sticky="w", padx=10,
                                                      pady=10)
        for i, n in enumerate(dests):
            row = ctk.CTkFrame(self.dest_list)
            row.grid(row=i, column=0, sticky="ew", pady=2, padx=2)
            row.grid_columnconfigure(0, weight=1)
            tls = "  · TLS" if n.tls else ""
            ctk.CTkLabel(row, text=f"{n.name}\n{n.aetitle}@{n.host}:{n.port}{tls}",
                         anchor="w", justify="left").grid(
                row=0, column=0, sticky="w", padx=10, pady=6)
            ctk.CTkButton(row, text="Edit", width=56,
                          command=lambda k=i: self._edit(k)).grid(row=0,
                                                                  column=1, padx=2)
            ctk.CTkButton(row, text="Delete", width=64, fg_color=theme.DANGER,
                          command=lambda k=i: self._delete(k)).grid(
                row=0, column=2, padx=(2, 8))
        self._refresh_roles()

    def _refresh_roles(self):
        names = [n.name for n in self.app.destinations]
        for menu, cur in ((self.query_role, self.settings.query_destination),
                          (self.send_role, self.settings.send_destination)):
            if names:
                menu.configure(values=names, state="normal")
                menu.set(cur if cur in names else names[0])
            else:
                menu.configure(values=["(none)"], state="disabled")
                menu.set("(none)")

    def _save_roles(self):
        if self.app.destinations:
            self.settings.query_destination = self.query_role.get()
            self.settings.send_destination = self.send_role.get()
            config.save_settings(self.settings)
            self._notify()
            self._flash("Saved.")

    def _add(self):
        def on_save(node):
            self.app.destinations.append(node)
            config.save_destinations(self.app.destinations)
            self._refresh_dest_list()
            self._notify()
            self._flash("Destination added.")
        AddDestinationDialog(self, on_save)

    def _edit(self, idx):
        node = self.app.destinations[idx]

        def on_save(new_node):
            self.app.destinations[idx] = new_node
            config.save_destinations(self.app.destinations)
            self._refresh_dest_list()
            self._notify()
            self._flash("Destination updated.")
        AddDestinationDialog(self, on_save, initial=node)

    def _delete(self, idx):
        node = self.app.destinations[idx]
        if not messagebox.askyesno("Delete destination",
                                   f"Remove '{node.name}'?"):
            return
        del self.app.destinations[idx]
        config.save_destinations(self.app.destinations)
        self._refresh_dest_list()
        self._notify()
        self._flash("Destination deleted.")

    # ------------------------------------------------------------------- common
    def _notify(self):
        if self.on_changed:
            try:
                self.on_changed()
            except Exception:  # noqa: BLE001
                pass

    def _flash(self, text, error=False):
        self.status.configure(text=text,
                              text_color="#d9534f" if error else theme.MUTED)

    def _close(self):
        self._save_app()
        self.grab_release()
        self.destroy()
