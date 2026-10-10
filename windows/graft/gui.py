"""Small, standard-library-only Windows manager. All Tk work stays on its thread."""

from __future__ import annotations

import queue
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import platform

AUTO_MERGE_MS = 120_000


def shortcut_command(profile_id: str, claude: str | None = None,
                     data_dir: Path | None = None) -> tuple[Path, list[str]]:
    """Use absolute paths so shortcuts work independently of their working directory."""
    executable = Path(sys.executable).resolve()
    arguments: list[str] = []
    if not getattr(sys, "frozen", False):
        pythonw = executable.with_name("pythonw.exe")
        if sys.platform == "win32" and pythonw.is_file():
            executable = pythonw
        arguments.append(str(Path(__file__).resolve().parent.parent / "launch.py"))
    arguments.extend(["--open", profile_id])
    if claude:
        arguments.extend(["--claude", str(Path(claude).expanduser().resolve())])
    if data_dir is not None:
        arguments.extend(["--data-dir", str(data_dir.expanduser().resolve())])
    return executable, arguments


def merge_summary(result: dict) -> str:
    summary = (
        f"Merge finished: {result.get('copied', 0)} copied, "
        f"{result.get('deleted', 0)} deleted, "
        f"{result.get('conflicts', 0)} conflicts, "
        f"{result.get('backups', 0)} backups."
    )
    if result.get("recovered"):
        summary = "Recovered an interrupted merge; counts below are new changes after recovery.\n" + summary
    if result.get("conflicts"):
        summary += " Conflicting records were left unchanged; review them before another merge."
    warnings = result.get("warnings", [])
    if warnings:
        summary += "\n" + "\n".join(str(warning) for warning in warnings)
    return summary


class GroupDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, profiles: list[dict], group: dict | None = None):
        self.profiles = profiles
        self.group = group
        self.result = None
        super().__init__(parent, "Edit chat group" if group else "New chat group")

    def body(self, master):
        ttk.Label(master, text="Group name" + (" (fixed after creation)" if self.group else "")).grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        self.name = ttk.Entry(master, width=44)
        self.name.grid(row=1, column=0, sticky="ew")
        if self.group:
            self.name.insert(0, self.group["name"])
            self.name.state(["readonly"])
        ttk.Label(master, text="Choose at least two profiles. Main Claude is optional.\n"
                  "Use Ctrl-click or Shift-click to select multiple profiles.").grid(
            row=2, column=0, sticky="w", pady=(14, 6))
        list_frame = ttk.Frame(master)
        list_frame.grid(row=3, column=0, sticky="nsew")
        self.members = tk.Listbox(list_frame, selectmode=tk.EXTENDED, exportselection=False,
                                  height=min(12, max(4, len(self.profiles))), width=48)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.members.yview)
        self.members.configure(yscrollcommand=scroll.set)
        self.members.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        selected = set(self.group["members"]) if self.group else set()
        for index, profile in enumerate(self.profiles):
            self.members.insert(tk.END, profile["name"])
            if profile["id"] in selected:
                self.members.selection_set(index)
        ttk.Label(master, text="A profile can belong to one chat group at a time.",
                  wraplength=390).grid(row=4, column=0, sticky="w", pady=(10, 0))
        return self.members if self.group else self.name

    def validate(self):
        if not self.name.get().strip():
            messagebox.showerror("Group name required", "Enter a name for this group.", parent=self)
            return False
        if len(self.members.curselection()) < 2:
            messagebox.showerror("Choose profiles", "Choose at least two profiles.", parent=self)
            return False
        return True

    def apply(self):
        self.result = (self.name.get().strip(),
                       [self.profiles[index]["id"] for index in self.members.curselection()])


class App:
    def __init__(self, root: tk.Tk, manager, claude: str | None = None,
                 data_dir: Path | None = None):
        self.root = root
        self.manager = manager
        self.data_dir = data_dir
        self.busy = False
        self.modal = False
        self.closed = False
        self.results: queue.Queue = queue.Queue()
        self.controls: list = []
        self.profile_rows: list[dict] = []
        self.group_rows: list[dict] = []
        self.claude = tk.StringVar(value=claude or "")
        self.automatic = tk.BooleanVar(value=manager.auto_merge())
        self.status = tk.StringVar(value="Ready. Close every Claude window and tray process before merging.")
        root.title("Claude Graft for Windows")
        root.geometry("860x680")
        root.minsize(740, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.refresh()
        self.poll_id = root.after(100, self._poll)
        self.auto_id = root.after(AUTO_MERGE_MS, self._auto_merge)

    def _button(self, parent, title, command):
        button = ttk.Button(parent, text=title, command=command)
        self.controls.append(button)
        button.pack(side="left", padx=(0, 8))
        return button

    def _build(self):
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Claude Graft", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Separate profiles and experimental local chat-record sharing.").pack(
            anchor="w", pady=(2, 12))
        notice = ttk.Label(frame, text="Windows companion (experimental). Sign in separately to each profile. "
                           "Sidebar display and cross-account resume depend on Claude and are not guaranteed.", wraplength=790)
        notice.pack(anchor="w", pady=(0, 12))
        exe_frame = ttk.Frame(frame)
        exe_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(exe_frame, text="Claude.exe (optional):").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(exe_frame, textvariable=self.claude)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.controls.append(entry)
        self._button(exe_frame, "Browse…", self.browse_claude)

        profiles = ttk.LabelFrame(frame, text="Profiles", padding=10)
        profiles.pack(fill="both", expand=True)
        self.profiles = self._tree(profiles, columns=("name", "folder"), show="headings", height=5,
                                     selectmode="browse")
        self.profiles.heading("name", text="Profile")
        self.profiles.heading("folder", text="Folder")
        self.profiles.column("name", width=190)
        self.profiles.column("folder", width=420)
        self.profiles.bind("<Double-1>", lambda event: self.open_profile())
        actions = ttk.Frame(profiles)
        actions.pack(fill="x", pady=(8, 0))
        self._button(actions, "New profile…", self.add_profile)
        self._button(actions, "Open Claude", self.open_profile)
        self._button(actions, "Create shortcut…", self.create_shortcut)
        self._button(actions, "Unregister…", self.remove_profile)

        groups = ttk.LabelFrame(frame, text="Chat groups", padding=10)
        groups.pack(fill="both", expand=True, pady=(12, 0))
        self.groups = self._tree(groups, columns=("name", "members"), show="headings", height=4,
                                   selectmode="browse")
        self.groups.heading("name", text="Group")
        self.groups.heading("members", text="Sharing history")
        self.groups.column("name", width=190)
        self.groups.column("members", width=420)
        self.groups.bind("<Double-1>", lambda event: self.edit_group())
        group_actions = ttk.Frame(groups)
        group_actions.pack(fill="x", pady=(8, 0))
        self._button(group_actions, "New group…", lambda: self.edit_group(new=True))
        self._button(group_actions, "Edit…", self.edit_group)
        self._button(group_actions, "Remove…", self.remove_group)

        merge_actions = ttk.Frame(frame)
        merge_actions.pack(fill="x", pady=(14, 6))
        self._button(merge_actions, "Merge chats now", self.merge)
        self._button(merge_actions, "Refresh", self.refresh)
        auto = ttk.Checkbutton(merge_actions, text="Merge every 2 minutes while this manager is open",
                               variable=self.automatic, command=self.set_automatic)
        auto.pack(side="left")
        self.controls.append(auto)
        ttk.Label(frame, textvariable=self.status, wraplength=785, justify="left").pack(
            anchor="w", fill="x", pady=(6, 0))

    @staticmethod
    def _tree(parent, **options):
        # The holder must be the tree's actual parent. Packing an older sibling
        # with in_=holder leaves it below that frame in the native stacking
        # order: rows still exist, but the frame covers their pixels and clicks.
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        tree = ttk.Treeview(holder, **options)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    @contextmanager
    def _dialog(self):
        self.modal = True
        try:
            yield
        finally:
            self.modal = False

    def refresh(self):
        if self.busy:
            return
        try:
            selected_profile = self.profiles.selection()
            selected_group = self.groups.selection()
            self.profile_rows = [{"id": "main", "name": "Main Claude", "folder": str(self.manager.profile_path("main"))}]
            self.profile_rows.extend(profile for profile in self.manager.profiles() if profile["id"] != "main")
            self.group_rows = self.manager.groups()
            names = {profile["id"]: profile["name"] for profile in self.profile_rows}
            for tree in (self.profiles, self.groups):
                tree.delete(*tree.get_children())
            for profile in self.profile_rows:
                self.profiles.insert("", "end", iid=profile["id"], values=(profile["name"], profile["folder"]))
            for group in self.group_rows:
                members = ", ".join(names.get(member, member) for member in group["members"])
                self.groups.insert("", "end", iid=group["id"], values=(group["name"], members))
            for tree, selection in ((self.profiles, selected_profile), (self.groups, selected_group)):
                if selection and tree.exists(selection[0]):
                    tree.selection_set(selection[0])
            if not self.profiles.selection():
                self.profiles.selection_set("main")
        except Exception as exc:
            self._error(exc)

    def _selected_profile(self):
        selected = self.profiles.selection()
        return next((profile for profile in self.profile_rows if selected and profile["id"] == selected[0]), None)

    def _selected_group(self):
        selected = self.groups.selection()
        return next((group for group in self.group_rows if selected and group["id"] == selected[0]), None)

    def _error(self, exc):
        message = " ".join(str(exc).split()) or "The operation failed."
        self.status.set(message)
        with self._dialog():
            messagebox.showerror("Claude Graft", message, parent=self.root)

    def _run(self, description: str, work: Callable, finished: Callable | None = None,
             quiet: bool = False):
        if self.busy or self.closed:
            return
        self.busy = True
        self.status.set(description)
        for widget in self.controls:
            widget.state(["disabled"])

        def worker():
            try:
                self.results.put((True, work(), finished, quiet))
            except Exception as exc:
                self.results.put((False, exc, finished, quiet))

        # Do not abandon a filesystem transaction when the window is closed.
        threading.Thread(target=worker, name="graft-operation", daemon=False).start()

    def _poll(self):
        if self.closed:
            return
        try:
            success, value, finished, quiet = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for widget in self.controls:
                widget.state(["!disabled"])
            if success:
                self.refresh()
                if finished:
                    finished(value)
                else:
                    self.status.set("Done.")
            elif quiet:
                self.status.set(f"Automatic merge skipped: {' '.join(str(value).split())}")
            else:
                self._error(value)
        self.poll_id = self.root.after(100, self._poll)

    def browse_claude(self):
        if self.busy:
            return
        with self._dialog():
            filename = filedialog.askopenfilename(parent=self.root, title="Choose Claude.exe",
                                                   filetypes=[("Windows executable", "*.exe")])
        if filename:
            self.claude.set(filename)

    def add_profile(self):
        if self.busy:
            return
        with self._dialog():
            name = simpledialog.askstring("New profile", "Profile name:", parent=self.root)
        if name is None:
            return
        if not name.strip():
            self._error(ValueError("Enter a profile name."))
            return

        def done(profile):
            self.profiles.selection_set(profile["id"])
            self.status.set(f"Created {profile['name']}. Open it and sign in before merging chats.")

        self._run("Creating profile…", lambda: self.manager.add_profile(name.strip()), done)

    def open_profile(self):
        if self.busy:
            return
        profile = self._selected_profile()
        if not profile:
            return
        executable = self.claude.get().strip() or None
        self._run(f"Opening {profile['name']}…",
                  lambda: self.manager.open_profile(profile["id"], executable=executable),
                  lambda _: self.status.set(f"Opened {profile['name']}."))

    def create_shortcut(self):
        if self.busy:
            return
        profile = self._selected_profile()
        if not profile:
            return
        safe_name = "".join(char for char in profile["name"] if char not in '<>:"/\\|?*' and ord(char) >= 32)
        with self._dialog():
            filename = filedialog.asksaveasfilename(
                parent=self.root, title="Save profile shortcut", defaultextension=".lnk",
                filetypes=[("Windows shortcut", "*.lnk")], initialfile=f"Claude - {safe_name or 'Profile'}.lnk")
        if not filename:
            return
        executable, arguments = shortcut_command(profile["id"], self.claude.get().strip() or None, self.data_dir)
        self._run("Creating shortcut…", lambda: platform.create_shortcut(
            profile["name"], executable, arguments, Path(filename)),
            lambda path: self.status.set(f"Shortcut saved: {path}"))

    def remove_profile(self):
        if self.busy:
            return
        profile = self._selected_profile()
        if not profile:
            return
        if profile["id"] == "main":
            self._error(ValueError("Main Claude cannot be unregistered."))
            return
        with self._dialog():
            confirmed = messagebox.askyesno(
                "Unregister profile", f"Unregister {profile['name']} and remove its group memberships?\n\n"
                "Its files and sign-in data will stay on disk. Existing shortcuts will stop working.", parent=self.root)
        if confirmed:
            self._run("Unregistering profile…", lambda: self.manager.remove_profile(profile["id"]))

    def edit_group(self, new=False):
        if self.busy:
            return
        group = None if new else self._selected_group()
        if not new and group is None:
            return
        with self._dialog():
            dialog = GroupDialog(self.root, self.profile_rows, group)
        if dialog.result:
            name, members = dialog.result
            if new and any(existing["name"].casefold() == name.casefold() for existing in self.group_rows):
                self._error(ValueError("A group with that name already exists. Select it and choose Edit."))
                return
            self._run("Saving group…", lambda: self.manager.set_group(name, members))

    def remove_group(self):
        if self.busy:
            return
        group = self._selected_group()
        if group is None:
            return
        with self._dialog():
            confirmed = messagebox.askyesno(
                "Remove chat group", f"Remove {group['name']}?\n\nAlready shared chats will remain in each profile.",
                parent=self.root)
        if confirmed:
            self._run("Removing group…", lambda: self.manager.remove_group(group["id"]))

    def set_automatic(self):
        if self.busy:
            return
        enabled = self.automatic.get()
        try:
            self.manager.set_auto_merge(enabled)
        except Exception as exc:
            self.automatic.set(not enabled)
            self._error(exc)
        else:
            self.status.set("Automatic merging enabled. Claude is never closed automatically." if enabled
                            else "Automatic merging disabled.")

    def merge(self, automatic=False):
        if self.busy or self.modal:
            return

        def done(result):
            self.status.set(" ".join(merge_summary(result).splitlines()[:2 if result.get("recovered") else 1]))
            if not automatic or result.get("conflicts") or result.get("warnings"):
                with self._dialog():
                    show = messagebox.showwarning if result.get("conflicts") or result.get("warnings") else messagebox.showinfo
                    show("Chat merge result", merge_summary(result), parent=self.root)

        self._run("Merging chats… Keep Claude closed until this finishes.", self.manager.merge, done, quiet=automatic)

    def _auto_merge(self):
        if self.closed:
            return
        self.auto_id = self.root.after(AUTO_MERGE_MS, self._auto_merge)
        if self.automatic.get() and not self.busy and not self.modal:
            self.merge(automatic=True)

    def close(self):
        if self.busy:
            self.status.set("An operation is still running. Wait until it finishes before closing Claude Graft.")
            return
        self.closed = True
        self.root.after_cancel(self.poll_id)
        self.root.after_cancel(self.auto_id)
        self.root.destroy()


def run(manager, claude: str | None = None, data_dir: Path | None = None) -> None:
    root = tk.Tk()
    try:
        App(root, manager, claude=claude, data_dir=data_dir)
        root.mainloop()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
