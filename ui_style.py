"""Shared styles and progress controls for the desktop tabs."""

import tkinter as tk

import customtkinter as ctk


CANVAS = ("#eaf0f8", "#151e2c")
SURFACE = ("#fbfdff", "#202c3d")
BORDER = ("#cad6e6", "#3c4d65")
INK = ("#14243d", "#edf4ff")
MUTED = ("#53637a", "#b3c1d4")
TABLE_HEADER = ("#dfe8f6", "#293b54")
ACCENT = ("#155bc2", "#2871d5")
ACCENT_HOVER = ("#104ca8", "#1d60bb")
SELECTED = ("#e0edff", "#344e71")
STRIPE = ("#f4f8fd", "#243247")
BANNER = ("#e0edfd", "#203b58")

_FIELD = ("#ffffff", "#1a2535")
_DISABLED = ("#667386", "#9aaabd")
_HEADING_FONT = ("Segoe UI", 19, "bold")
_TEXTBOX_STYLE = {
    "fg_color": _FIELD,
    "border_color": BORDER,
    "border_width": 1,
    "corner_radius": 8,
    "text_color": INK,
    "font": ("Segoe UI", 13),
    "padx": 4,
    "pady": 2,
}


def card(master, **kwargs):
    options = {"fg_color": SURFACE, "border_color": BORDER, "border_width": 1, "corner_radius": 12}
    options.update(kwargs)
    return ctk.CTkFrame(master, **options)


def section_title(master, text):
    return ctk.CTkLabel(master, text=text, anchor="w", text_color=INK, font=_HEADING_FONT)


def primary_button(master, **kwargs):
    options = {
        "fg_color": ACCENT,
        "hover_color": ACCENT_HOVER,
        "text_color": "#ffffff",
        "height": 35,
        "corner_radius": 8,
        "font": ("Segoe UI", 13, "bold"),
    }
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


def secondary_button(master, **kwargs):
    options = {
        "fg_color": SURFACE,
        "hover_color": SELECTED,
        "border_color": BORDER,
        "border_width": 1,
        "text_color": INK,
        "text_color_disabled": _DISABLED,
        "height": 35,
        "corner_radius": 8,
        "font": ("Segoe UI", 13),
    }
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


def stop_button(master, **kwargs):
    options = {
        "fg_color": TABLE_HEADER,
        "hover_color": ("#fde5e5", "#74383c"),
        "text_color": ("#8e2b31", "#ffb8ba"),
    }
    options.update(kwargs)
    return secondary_button(master, **options)


def textbox(master, *, placeholder=None, **kwargs):
    options = {**_TEXTBOX_STYLE, **kwargs}
    if placeholder is not None:
        return _PlaceholderTextbox(master, placeholder, **options)
    return ctk.CTkTextbox(master, **options)


def scroll_panel(master, **kwargs):
    options = {
        "fg_color": SURFACE,
        "border_width": 1,
        "border_color": BORDER,
        "corner_radius": 12,
        "label_fg_color": SURFACE,
        "label_text_color": INK,
        "label_font": _HEADING_FONT,
        "label_anchor": "w",
        "scrollbar_button_color": BORDER,
        "scrollbar_button_hover_color": MUTED,
    }
    options.update(kwargs)
    return ctk.CTkScrollableFrame(master, **options)


def empty_state(master, title, detail):
    frame = ctk.CTkFrame(master, fg_color="transparent")
    ctk.CTkLabel(frame, text=title, text_color=INK, font=("Segoe UI", 16, "bold")).pack()
    ctk.CTkLabel(
        frame, text=detail, text_color=MUTED, font=("Segoe UI", 13), wraplength=320
    ).pack(pady=(1, 0))
    return frame


class _PlaceholderTextbox(ctk.CTkTextbox):
    def __init__(self, master, placeholder, **kwargs):
        super().__init__(master, **kwargs)
        self._focused = False
        self._placeholder = ctk.CTkLabel(
            self, text=placeholder, text_color=MUTED, fg_color=self.cget("fg_color"), font=self.cget("font")
        )
        if self.cget("state") != "disabled":
            self.bind("<FocusIn>", lambda _event: self._set_focused(True))
            self.bind("<FocusOut>", lambda _event: self._set_focused(False))
            self._placeholder.bind("<Button-1>", lambda _event: self.focus_set())
        self._refresh_placeholder()

    def _set_focused(self, focused):
        self._focused = focused
        self._refresh_placeholder()

    def _refresh_placeholder(self):
        if self._focused or self.compare("end-1c", ">", "1.0"):
            self._placeholder.place_forget()
        else:
            self._placeholder.place(x=13, y=6, anchor="nw")
            self._placeholder.lift()

    def insert(self, index, text, tags=None):
        super().insert(index, text, tags)
        self._refresh_placeholder()

    def delete(self, index1, index2=None):
        super().delete(index1, index2)
        self._refresh_placeholder()


class _ActivityLog(_PlaceholderTextbox):
    def insert(self, index, text, tags=None):
        self.configure(state="normal")
        try:
            super().insert(index, text, tags)
        finally:
            self.configure(state="disabled")


def log_panel(master):
    panel = card(master)
    panel.grid_columnconfigure(0, weight=1)
    panel.grid_rowconfigure(1, weight=1)
    section_title(panel, "Nhật ký hoạt động").grid(row=0, column=0, padx=14, pady=(8, 6), sticky="ew")
    output = _ActivityLog(panel, "Chưa có hoạt động.", height=118, state="disabled", **_TEXTBOX_STYLE)
    output.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")
    return panel, output


def bind_row_menu(widgets, callback):
    for widget in widgets:
        for event in ("<ButtonRelease-1>", "<ButtonRelease-2>", "<ButtonRelease-3>"):
            widget.bind(event, callback)


def show_row_menu(master, event, entries):
    menu = getattr(master, "_copy_menu", None)
    if menu is None:
        menu = tk.Menu(master, tearoff=False)
        master._copy_menu = menu
    else:
        menu.delete(0, "end")
    for label, command in entries:
        menu.add_command(label=label, command=command)
    menu.add_separator()
    menu.add_command(label="Hủy", command=menu.unpost)
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return "break"
