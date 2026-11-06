"""Standalone Print2PACS wizard: Images -> Find study -> Send.

Designed for non-IT users: three clear steps, drag-and-drop, and a single
"Send to PACS" action. Heavy work (C-FIND / C-STORE) runs off the UI thread.
"""

from __future__ import annotations

import os
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except Exception:  # noqa: BLE001
    TkinterDnD = None
    DND_FILES = None
    _HAS_DND = False

from .. import APP_NAME, __version__, config, paths
from ..engine import reconcile, sc_import, send, video
from ..logging_setup import get_logger, setup as setup_logging
from ..model import CONVERSION_TYPES, SeriesInfo, StudyContext
from ..net import scu
from . import theme
from .dialogs import AddDestinationDialog
from .widgets import run_threaded

log = get_logger("gui")

_APP_BASES = (ctk.CTk, TkinterDnD.DnDWrapper) if _HAS_DND else (ctk.CTk,)


class App(*_APP_BASES):
    def __init__(self):
        super().__init__()
        self._dnd_ok = False
        if _HAS_DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self._dnd_ok = True
            except Exception:  # noqa: BLE001
                self._dnd_ok = False

        setup_logging()
        self.settings = config.load_settings()
        self.destinations = config.load_destinations()

        ctk.set_appearance_mode(self.settings.appearance)
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_NAME}  v{__version__}")
        self.minsize(760, 560)
        self.geometry("860x660")

        icon = paths.resource_dir() / "assets" / "icon.ico"
        if icon.exists():
            try:
                self.iconbitmap(str(icon))
            except Exception:  # noqa: BLE001
                pass

        # ---- shared wizard state ----
        self.pages: list[sc_import.SourcePage] = []
        # videos: list of (path, VideoMeta, pil_thumb_or_None)
        self.videos: list[tuple] = []
        self._thumb_imgs: list = []          # keep CTkImage refs alive
        self.studies: list[StudyContext] = []
        self.chosen_study: StudyContext | None = None
        self._step = 1
        self._sending = False

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._update_info = None
        self._build_header()
        self._build_content()
        self._build_nav()
        self._build_statusbar()
        self._show_step(1)

        # Quiet background update check (built exe only).
        from .. import updater
        if updater.can_self_update():
            self.after(2500, lambda: self.check_updates(manual=False))

    # ------------------------------------------------------------------ header
    def _build_header(self):
        head = ctk.CTkFrame(self, corner_radius=0, height=64)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self.step_title = ctk.CTkLabel(
            head, text="", font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w")
        self.step_title.grid(row=0, column=0, sticky="w", padx=18, pady=(12, 0))
        self.step_sub = ctk.CTkLabel(head, text="", text_color=theme.MUTED,
                                     anchor="w")
        self.step_sub.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))
        self.step_dots = ctk.CTkLabel(
            head, text="", font=ctk.CTkFont(size=13, weight="bold"),
            text_color=theme.MUTED)
        self.step_dots.grid(row=0, column=1, rowspan=2, sticky="e", padx=(18, 6))
        ctk.CTkButton(head, text="⚙ Settings", width=100, fg_color="gray",
                      command=self._open_settings).grid(
            row=0, column=2, rowspan=2, sticky="e", padx=(0, 14))

    # ----------------------------------------------------------------- content
    def _build_content(self):
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self.frames: dict[int, ctk.CTkFrame] = {}
        self.frames[1] = self._build_step1()
        self.frames[2] = self._build_step2()
        self.frames[3] = self._build_step3()
        for f in self.frames.values():
            f.grid(row=0, column=0, sticky="nsew")

    # --------- Step 1: images ---------
    def _build_step1(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        self.drop = ctk.CTkFrame(f, height=90, fg_color=theme.DROP_IDLE,
                                 corner_radius=10)
        self.drop.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.drop.grid_propagate(False)
        msg = ("Drag images or a PDF here  —  or  "
               if self._dnd_ok else "")
        inner = ctk.CTkFrame(self.drop, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(inner, text=msg, text_color=theme.MUTED).pack(side="left")
        ctk.CTkButton(inner, text="Add files…", width=120,
                      command=self._add_files).pack(side="left")
        ctk.CTkLabel(f, text="Images: JPG · PNG · TIFF · BMP · PDF      "
                             "Video: MP4 · MOV · AVI · MKV",
                     text_color=theme.MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0,
                                                     sticky="s", pady=(0, 2))

        self.thumb_list = ctk.CTkScrollableFrame(f, label_text="Pages to send")
        self.thumb_list.grid(row=1, column=0, sticky="nsew")
        self.thumb_list.grid_columnconfigure(0, weight=1)

        self.count_lbl = ctk.CTkLabel(f, text="Pages to send: 0",
                                      font=ctk.CTkFont(weight="bold"))
        self.count_lbl.grid(row=2, column=0, sticky="w", pady=(8, 0))

        if self._dnd_ok:
            self._enable_drop(self.drop, self._on_drop)
            self._enable_drop(self.thumb_list, self._on_drop)
        return f

    # --------- Step 2: find study ---------
    def _build_step2(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(2, weight=1)

        dest = ctk.CTkFrame(f)
        dest.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        dest.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dest, text="Query PACS:").grid(row=0, column=0, padx=12,
                                                    pady=12)
        self.query_menu = ctk.CTkOptionMenu(dest, values=["(none)"],
                                            command=lambda _v: None)
        self.query_menu.grid(row=0, column=1, sticky="w", padx=6, pady=12)
        ctk.CTkButton(dest, text="＋ Add PACS…", width=110,
                      command=self._add_destination).grid(row=0, column=2,
                                                          padx=6)
        ctk.CTkButton(dest, text="Test", width=60, fg_color="gray",
                      command=self._test_connection).grid(row=0, column=3,
                                                          padx=(6, 12))

        search = ctk.CTkFrame(f)
        search.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        search.grid_columnconfigure((1, 3), weight=1)
        ctk.CTkLabel(search, text="Accession #:").grid(row=0, column=0,
                                                       padx=12, pady=12)
        self.acc_var = ctk.StringVar()
        acc = ctk.CTkEntry(search, textvariable=self.acc_var)
        acc.grid(row=0, column=1, sticky="ew", padx=6, pady=12)
        acc.bind("<Return>", lambda _e: self._lookup())
        ctk.CTkLabel(search, text="Patient ID:").grid(row=0, column=2,
                                                      padx=12, pady=12)
        self.pid_var = ctk.StringVar()
        pid = ctk.CTkEntry(search, textvariable=self.pid_var)
        pid.grid(row=0, column=3, sticky="ew", padx=6, pady=12)
        pid.bind("<Return>", lambda _e: self._lookup())
        ctk.CTkButton(search, text="Look up", width=100,
                      command=self._lookup).grid(row=0, column=4,
                                                 padx=(6, 12))

        self.results = ctk.CTkScrollableFrame(f, label_text="Matching study")
        self.results.grid(row=2, column=0, sticky="nsew")
        self.results.grid_columnconfigure(0, weight=1)

        self.find_status = ctk.CTkLabel(f, text="", text_color=theme.MUTED,
                                        anchor="w", wraplength=760,
                                        justify="left")
        self.find_status.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        return f

    # --------- Step 3: series + send ---------
    def _build_step3(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)

        self.confirm_box = ctk.CTkFrame(f)
        self.confirm_box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.confirm_box.grid_columnconfigure(1, weight=1)
        self.confirm_lbl = ctk.CTkLabel(self.confirm_box, text="", anchor="w",
                                        justify="left", wraplength=780)
        self.confirm_lbl.grid(row=0, column=0, columnspan=2, sticky="w",
                              padx=14, pady=(12, 6))
        ctk.CTkLabel(self.confirm_box, text="Send to PACS:").grid(
            row=1, column=0, sticky="w", padx=14, pady=(0, 12))
        self.send_menu = ctk.CTkOptionMenu(self.confirm_box, values=["(none)"],
                                           command=lambda _v: None)
        self.send_menu.grid(row=1, column=1, sticky="w", padx=6, pady=(0, 12))

        series = ctk.CTkFrame(f)
        series.grid(row=1, column=0, sticky="new", pady=(0, 10))
        series.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(series, text="New series details",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 4))

        ctk.CTkLabel(series, text="Modality:").grid(row=1, column=0, sticky="w",
                                                    padx=12, pady=6)
        self.modality_var = ctk.StringVar(value=self.settings.default_modality)
        ctk.CTkOptionMenu(series, variable=self.modality_var,
                          values=["OT", "SC", "XC", "DOC", "CR", "DX"],
                          width=120).grid(row=1, column=1, sticky="w", padx=6)

        ctk.CTkLabel(series, text="Series description:").grid(
            row=2, column=0, sticky="w", padx=12, pady=6)
        self.desc_var = ctk.StringVar(
            value=self.settings.default_series_description)
        ctk.CTkEntry(series, textvariable=self.desc_var).grid(
            row=2, column=1, sticky="ew", padx=6, pady=6)

        ctk.CTkLabel(series, text="Body part:").grid(row=3, column=0,
                                                     sticky="w", padx=12, pady=6)
        self.body_var = ctk.StringVar()
        ctk.CTkEntry(series, textvariable=self.body_var).grid(
            row=3, column=1, sticky="ew", padx=6, pady=6)

        ctk.CTkLabel(series, text="Capture type:").grid(row=4, column=0,
                                                        sticky="w", padx=12,
                                                        pady=6)
        ct_values = [f"{k} - {v}" for k, v in CONVERSION_TYPES.items()]
        self.conv_var = ctk.StringVar(
            value=next(v for v in ct_values
                       if v.startswith(self.settings.default_conversion_type)))
        ctk.CTkOptionMenu(series, variable=self.conv_var, values=ct_values,
                          width=220).grid(row=4, column=1, sticky="w", padx=6,
                                          pady=(6, 12))

        prog = ctk.CTkFrame(f, fg_color="transparent")
        prog.grid(row=2, column=0, sticky="ew")
        prog.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(prog)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", pady=(4, 2))
        self.send_status = ctk.CTkLabel(prog, text="", text_color=theme.MUTED,
                                        anchor="w", wraplength=780,
                                        justify="left")
        self.send_status.grid(row=1, column=0, sticky="ew")
        return f

    # -------------------------------------------------------------------- nav
    def _build_nav(self):
        bar = ctk.CTkFrame(self, corner_radius=0, height=56)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.back_btn = ctk.CTkButton(bar, text="‹ Back", width=90,
                                      fg_color="gray", command=self._back)
        self.back_btn.grid(row=0, column=1, padx=6, pady=10)
        self.next_btn = ctk.CTkButton(bar, text="Next ›", width=140,
                                      fg_color=theme.ACCENT,
                                      hover_color=theme.ACCENT_HOVER,
                                      command=self._next)
        self.next_btn.grid(row=0, column=2, padx=(6, 14), pady=10)

    # ------------------------------------------------------------- status bar
    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, corner_radius=0, height=26)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(bar, text=f"Data: {paths.data_dir()}",
                     text_color=theme.MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0,
                                                     sticky="w", padx=8)
        self.update_btn = ctk.CTkButton(
            bar, text="", height=20, fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._start_update)
        # shown only when an update is found
        ctk.CTkLabel(bar, text=f"v{__version__}", text_color=theme.MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=2,
                                                     sticky="e", padx=8)

    # ---------------------------------------------------------------- updates
    def check_updates(self, manual: bool = False):
        from .. import updater
        if manual and not updater.can_self_update():
            messagebox.showinfo(
                APP_NAME, f"You're on v{__version__}.\n\nAutomatic self-update "
                "works in the built .exe. From source, update with git.")
            return

        def work():
            return updater.check_latest()

        def done(info):
            self._on_update_result(info, manual)

        def err(exc):
            if manual:
                messagebox.showinfo(APP_NAME, f"Update check failed:\n{exc}")

        run_threaded(self, work, done, err)

    def _on_update_result(self, info, manual: bool):
        from .. import updater
        self._update_info = info
        if info:
            self.update_btn.configure(text=f"⬆ Update to v{info.version}")
            self.update_btn.grid(row=0, column=1, sticky="e", padx=8, pady=2)
            if manual and messagebox.askyesno(
                    "Update available",
                    f"Version {info.version} is available (you have "
                    f"{updater.current_version()}).\n\nInstall now? The app will "
                    "restart; your settings and destinations are kept."):
                self._start_update()
        elif manual:
            messagebox.showinfo(
                APP_NAME, f"You're on the latest version (v{__version__}).")

    def _start_update(self):
        from .. import updater
        info = self._update_info
        if not info:
            return
        if not updater.can_self_update():
            messagebox.showinfo(
                APP_NAME, f"Version {info.version} is available. Update the "
                "built .exe to install it (self-update needs the built exe).")
            return
        if not messagebox.askyesno(
                "Install update",
                f"Download and install v{info.version}? The app will close and "
                "reopen on the new version. Your settings are preserved."):
            return

        top = ctk.CTkToplevel(self)
        top.title("Updating")
        top.geometry("360x110")
        top.transient(self)
        ctk.CTkLabel(top, text=f"Downloading v{info.version}…").pack(pady=(18, 6))
        pbar = ctk.CTkProgressBar(top, width=300)
        pbar.set(0)
        pbar.pack(pady=6)

        def prog(frac):
            try:
                self.after(0, lambda: pbar.set(frac))
            except Exception:  # noqa: BLE001
                pass

        def work():
            updater.apply_update(info, progress=prog)
            self.after(0, self.destroy)   # exit so the helper can swap the exe

        def err(exc):
            try:
                top.destroy()
            except Exception:  # noqa: BLE001
                pass
            messagebox.showerror("Update failed", str(exc))

        run_threaded(self, work, None, err)

    # =====================================================================
    # Navigation
    # =====================================================================
    _TITLES = {
        1: ("Step 1 of 3 — Select images",
            "Add the pages you want to send. Drag them in or browse."),
        2: ("Step 2 of 3 — Find the study",
            "Look up the existing study in PACS by accession number."),
        3: ("Step 3 of 3 — Review and send",
            "Confirm the patient, set the series details, and send."),
    }

    def _show_step(self, n: int):
        self._step = n
        self.frames[n].tkraise()
        title, sub = self._TITLES[n]
        self.step_title.configure(text=title)
        self.step_sub.configure(text=sub)
        self.step_dots.configure(
            text="  ".join(("●" if i == n else "○") for i in (1, 2, 3)))
        self.back_btn.configure(state="normal" if n > 1 else "disabled")
        if n == 2:
            self._refresh_dest_menus()
        if n == 3:
            self._refresh_dest_menus()
            self.next_btn.configure(text="Send to PACS ⟶")
            self._refresh_confirm()
        else:
            self.next_btn.configure(text="Next ›")

    def _back(self):
        if self._step > 1 and not self._sending:
            self._show_step(self._step - 1)

    def _next(self):
        if self._step == 1:
            if not self.pages and not self.videos:
                messagebox.showwarning(APP_NAME,
                                       "Add at least one image or video first.")
                return
            self._show_step(2)
        elif self._step == 2:
            if not self.chosen_study:
                messagebox.showwarning(
                    APP_NAME, "Look up and select a study first.")
                return
            self._show_step(3)
        elif self._step == 3:
            self._send()

    # =====================================================================
    # Step 1 — images
    # =====================================================================
    @staticmethod
    def _is_accepted(p):
        return sc_import.is_supported(p) or video.is_video(p)

    def _add_files(self):
        img = " ".join(f"*{e}" for e in sorted(sc_import.SUPPORTED_EXTS))
        vid = " ".join(f"*{e}" for e in sorted(video.VIDEO_EXTS))
        files = filedialog.askopenfilenames(
            title="Select images, PDF, or video",
            filetypes=[("Images / PDF", img), ("Video", vid),
                       ("All files", "*.*")])
        if files:
            self._ingest(list(files))

    def _on_drop(self, paths_list):
        self._ingest(paths_list)

    def _ingest(self, file_paths):
        accepted = [p for p in file_paths if self._is_accepted(p)]
        skipped = len(file_paths) - len(accepted)
        if not accepted:
            messagebox.showwarning(
                APP_NAME, "No supported files. Accepted: images (JPG/PNG/TIFF/"
                "BMP/PDF) and video (MP4/MOV/AVI/MKV).")
            return
        img_paths = [p for p in accepted if sc_import.is_supported(p)]
        vid_paths = [p for p in accepted if video.is_video(p)]

        def work():
            pages = sc_import.load_many(img_paths, self.settings.pdf_dpi)
            vids = []
            for vp in vid_paths:
                meta = video.probe(vp)            # runs ffmpeg
                thumb = video.first_frame(vp)
                vids.append((vp, meta, thumb))
            return pages, vids

        def done(result):
            pages, vids = result
            self.pages.extend(pages)
            self.videos.extend(vids)
            self._refresh_thumbs()
            if skipped:
                self.count_lbl.configure(
                    text=f"{self._item_count()} item(s)  "
                         f"({skipped} unsupported skipped)")

        def err(exc):
            messagebox.showerror(
                APP_NAME, f"Could not load files:\n{exc}\n\n"
                "(Video needs ffmpeg — run scripts/fetch_ffmpeg.py or install it.)")
            self._refresh_thumbs()

        self.count_lbl.configure(text="Loading…")
        run_threaded(self, work, done, err)

    def _item_count(self):
        return len(self.pages) + len(self.videos)

    def _refresh_thumbs(self):
        for w in self.thumb_list.winfo_children():
            w.destroy()
        self._thumb_imgs = []
        for i, page in enumerate(self.pages):
            row = ctk.CTkFrame(self.thumb_list)
            row.grid(row=i, column=0, sticky="ew", pady=3, padx=2)
            row.grid_columnconfigure(1, weight=1)

            t = page.image.copy()
            t.thumbnail((84, 84))
            img = ctk.CTkImage(light_image=t, dark_image=t, size=t.size)
            self._thumb_imgs.append(img)
            ctk.CTkLabel(row, image=img, text="").grid(row=0, column=0,
                                                       rowspan=2, padx=8, pady=6)
            label = page.source
            if page.page_index:
                label += f"  (page {page.page_index + 1})"
            ctk.CTkLabel(row, text=f"{i + 1}.  {label}", anchor="w").grid(
                row=0, column=1, rowspan=2, sticky="w")

            ctk.CTkButton(row, text="▲", width=30,
                          command=lambda k=i: self._move(k, -1)).grid(
                row=0, column=2, padx=2)
            ctk.CTkButton(row, text="▼", width=30,
                          command=lambda k=i: self._move(k, 1)).grid(
                row=0, column=3, padx=2)
            ctk.CTkButton(row, text="✕", width=30, fg_color=theme.DANGER,
                          command=lambda k=i: self._remove(k)).grid(
                row=0, column=4, padx=(2, 8))

        base = len(self.pages)
        for j, (vpath, meta, thumb) in enumerate(self.videos):
            row = ctk.CTkFrame(self.thumb_list)
            row.grid(row=base + j, column=0, sticky="ew", pady=3, padx=2)
            row.grid_columnconfigure(1, weight=1)
            if thumb is not None:
                t = thumb.copy()
                t.thumbnail((84, 84))
                img = ctk.CTkImage(light_image=t, dark_image=t, size=t.size)
                self._thumb_imgs.append(img)
                ctk.CTkLabel(row, image=img, text="").grid(
                    row=0, column=0, rowspan=2, padx=8, pady=6)
            else:
                ctk.CTkLabel(row, text="🎞", width=84,
                             font=ctk.CTkFont(size=28)).grid(
                    row=0, column=0, rowspan=2, padx=8, pady=6)
            from pathlib import Path as _P
            secs = f"{meta.duration:.0f}s" if meta.duration else "?"
            ctk.CTkLabel(
                row, anchor="w", justify="left",
                text=f"🎞 {_P(vpath).name}\n{meta.width}x{meta.height} · "
                     f"{meta.nframes or '?'} frames · {secs} → video").grid(
                row=0, column=1, rowspan=2, sticky="w")
            ctk.CTkButton(row, text="✕", width=30, fg_color=theme.DANGER,
                          command=lambda k=j: self._remove_video(k)).grid(
                row=0, column=4, padx=(2, 8))

        n_img, n_vid = len(self.pages), len(self.videos)
        parts = []
        if n_img:
            parts.append(f"{n_img} image page(s)")
        if n_vid:
            parts.append(f"{n_vid} video(s)")
        self.count_lbl.configure(
            text="To send: " + (", ".join(parts) if parts else "nothing yet"))

    def _move(self, idx, delta):
        j = idx + delta
        if 0 <= j < len(self.pages):
            self.pages[idx], self.pages[j] = self.pages[j], self.pages[idx]
            self._refresh_thumbs()

    def _remove(self, idx):
        if 0 <= idx < len(self.pages):
            del self.pages[idx]
            self._refresh_thumbs()

    def _remove_video(self, idx):
        if 0 <= idx < len(self.videos):
            del self.videos[idx]
            self._refresh_thumbs()

    # =====================================================================
    # Step 2 — find study
    # =====================================================================
    _NONE = "(no PACS configured)"

    def _refresh_dest_menus(self):
        names = [n.name for n in self.destinations]
        self._apply_menu(self.query_menu, names,
                         self.settings.query_destination)
        self._apply_menu(self.send_menu, names,
                         self.settings.send_destination
                         or self.settings.query_destination)

    def _apply_menu(self, menu, names, want):
        if names:
            menu.configure(values=names, state="normal")
            if want in names:
                menu.set(want)
            elif menu.get() not in names:
                menu.set(names[0])
        else:
            menu.configure(values=[self._NONE], state="disabled")
            menu.set(self._NONE)

    def _node_by_name(self, name):
        for n in self.destinations:
            if n.name == name:
                return n
        return None

    def _query_node(self):
        return self._node_by_name(self.query_menu.get())

    def _send_node(self):
        return self._node_by_name(self.send_menu.get())

    def _add_destination(self):
        def on_save(node):
            self.destinations.append(node)
            config.save_destinations(self.destinations)
            self._refresh_dest_menus()
            self.query_menu.set(node.name)
        AddDestinationDialog(self, on_save)

    def _open_settings(self):
        from .settings_view import SettingsDialog
        SettingsDialog(self, on_changed=self._refresh_dest_menus)

    def _test_connection(self):
        node = self._query_node()
        if not node:
            messagebox.showinfo(APP_NAME, "Add a PACS destination first.")
            return
        self.find_status.configure(text="Testing connection…")

        def work():
            return scu.c_echo(self.settings.my_aetitle, node)

        def done(res):
            self.find_status.configure(text=res.message)
            if not res.success:
                messagebox.showwarning(APP_NAME, res.message)

        run_threaded(self, work, done,
                     lambda e: self.find_status.configure(text=str(e)))

    def _lookup(self):
        node = self._query_node()
        if not node:
            messagebox.showinfo(APP_NAME, "Add a PACS destination first.")
            return
        acc = self.acc_var.get().strip()
        pid = self.pid_var.get().strip()
        if not acc and not pid:
            messagebox.showwarning(
                APP_NAME, "Enter an accession number or a patient ID.")
            return
        self.settings.query_destination = node.name
        config.save_settings(self.settings)
        self.find_status.configure(text="Querying PACS…")
        self.chosen_study = None

        def work():
            return reconcile.lookup_studies(
                self.settings.my_aetitle, node, accession=acc, patient_id=pid)

        def done(studies):
            self.studies = studies
            self._render_results()

        def err(exc):
            self.find_status.configure(text=str(exc))
            self._clear_results()

        run_threaded(self, work, done, err)

    def _clear_results(self):
        for w in self.results.winfo_children():
            w.destroy()

    def _render_results(self):
        self._clear_results()
        if not self.studies:
            self.find_status.configure(
                text="No matching study found. Check the accession number.")
            return
        self.find_status.configure(
            text=f"{len(self.studies)} match(es). Select the correct study.")
        self._study_var = ctk.IntVar(value=0)
        for i, s in enumerate(self.studies):
            card = ctk.CTkFrame(self.results)
            card.grid(row=i, column=0, sticky="ew", pady=4, padx=2)
            card.grid_columnconfigure(1, weight=1)
            ctk.CTkRadioButton(
                card, text="", variable=self._study_var, value=i,
                command=self._pick_study, width=24).grid(
                row=0, column=0, rowspan=2, padx=(10, 6), pady=10)
            name = s.patient_name.replace("^", ", ")
            ctk.CTkLabel(card, text=f"{name}   (ID {s.patient_id})",
                         font=ctk.CTkFont(weight="bold"), anchor="w").grid(
                row=0, column=1, sticky="w", pady=(8, 0))
            detail = (f"Acc {s.accession} · {s.study_description or '—'} · "
                      f"{s.study_date or '—'} · DOB {s.birth_date or '—'} · "
                      f"Sex {s.sex or '—'} · {s.modalities or '—'}")
            ctk.CTkLabel(card, text=detail, text_color=theme.MUTED,
                         anchor="w").grid(row=1, column=1, sticky="w",
                                          pady=(0, 8))
        self._pick_study()

    def _pick_study(self):
        idx = self._study_var.get()
        if 0 <= idx < len(self.studies):
            self.chosen_study = self.studies[idx]

    # =====================================================================
    # Step 3 — review + send
    # =====================================================================
    def _refresh_confirm(self):
        s = self.chosen_study
        if not s:
            self.confirm_lbl.configure(text="(no study selected)")
            return
        name = s.patient_name.replace("^", ", ")
        bits = []
        if self.pages:
            bits.append(f"{len(self.pages)} image page(s)")
        if self.videos:
            bits.append(f"{len(self.videos)} video(s)")
        self.confirm_lbl.configure(text=(
            f"Patient:  {name}   (ID {s.patient_id})\n"
            f"Study:  Acc {s.accession} · {s.study_description or '—'} · "
            f"{s.study_date or '—'}\n"
            f"To send:  {', '.join(bits) or 'nothing'}"))

    def _series_info(self) -> SeriesInfo:
        conv = self.conv_var.get().split(" - ")[0].strip()
        return SeriesInfo(
            modality=self.modality_var.get().strip() or "OT",
            series_description=self.desc_var.get().strip(),
            body_part=self.body_var.get().strip(),
            series_number=self.settings.default_series_number,
            conversion_type=conv or "WSD",
        )

    def _send(self):
        if self._sending:
            return
        node = self._send_node()
        if not node or not self.chosen_study or (not self.pages
                                                 and not self.videos):
            messagebox.showwarning(APP_NAME,
                                   "Missing study, PACS, or items to send.")
            return
        si = self._series_info()
        if not si.series_description:
            messagebox.showwarning(APP_NAME, "Enter a series description.")
            return

        # persist defaults for next time
        self.settings.send_destination = node.name
        self.settings.default_modality = si.modality
        self.settings.default_series_description = si.series_description
        self.settings.default_conversion_type = si.conversion_type
        config.save_settings(self.settings)

        self._sending = True
        self.next_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        self.progress.set(0)
        self.send_status.configure(text="Converting images…")

        study = self.chosen_study
        pages = list(self.pages)
        videos = list(self.videos)
        my_ae = self.settings.my_aetitle
        job_dir = paths.jobs_dir() / f"job_{study.accession or 'x'}"

        def prog(msg):
            try:
                self.after(0, lambda: self.send_status.configure(text=msg))
            except Exception:  # noqa: BLE001
                pass

        def frac(v):
            try:
                self.after(0, lambda: self.progress.set(v))
            except Exception:  # noqa: BLE001
                pass

        def work():
            datasets = []
            if pages:
                datasets.extend(sc_import.convert_pages(pages, study, si))
            for vpath, _meta, _thumb in videos:
                prog(f"Transcoding video {os.path.basename(str(vpath))}…")
                # each video becomes its own new series
                datasets.append(video.convert_video(vpath, study, si))
            return send.send_series(my_ae, node, datasets, job_dir,
                                    progress=prog, on_frac=frac)

        def done(result):
            self._sending = False
            self.back_btn.configure(state="normal")
            self.next_btn.configure(state="normal")
            if result.failed == 0 and result.sent > 0:
                self.progress.set(1)
                messagebox.showinfo(
                    APP_NAME,
                    f"Sent {result.sent} object(s) to PACS successfully.\n\n"
                    f"New series added to accession {study.accession}.")
                self._reset_after_send()
            else:
                detail = "\n".join(result.errors[:8]) or "Unknown error."
                messagebox.showerror(
                    APP_NAME,
                    f"Sent {result.sent}, failed {result.failed}.\n\n{detail}")

        def err(exc):
            self._sending = False
            self.back_btn.configure(state="normal")
            self.next_btn.configure(state="normal")
            messagebox.showerror(APP_NAME, f"Send failed:\n{exc}")

        run_threaded(self, work, done, err)

    def _reset_after_send(self):
        self.pages = []
        self.videos = []
        self._refresh_thumbs()
        self.studies = []
        self.chosen_study = None
        self._clear_results()
        self.acc_var.set("")
        self.pid_var.set("")
        self.find_status.configure(text="")
        self.progress.set(0)
        self.send_status.configure(text="")
        self._show_step(1)

    # ------------------------------------------------------------------- misc
    def _enable_drop(self, widget, on_paths):
        try:
            widget.drop_target_register(DND_FILES)

            def _drop(e):
                on_paths(list(self.tk.splitlist(e.data)))
            widget.dnd_bind("<<Drop>>", _drop)
        except Exception:  # noqa: BLE001
            pass


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
