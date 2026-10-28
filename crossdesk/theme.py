"""Small, local ttk theme. Uses Tk images, with no extra runtime dependency."""
import math
import tkinter as tk
from tkinter import ttk

BG = "#f5f7fb"
CARD = "#ffffff"
INK = "#202c40"
MUTED = "#7a879a"
BLUE = "#3d70ed"
GREEN = "#13a484"


def _surface(root, fill, border=None):
    image = tk.PhotoImage(master=root, width=64, height=42)
    def layer(color, inset):
        radius = 10 - inset
        for y in range(inset, 42-inset):
            cy = min(y-inset, 41-inset-y)
            corner = radius-math.sqrt(max(0, radius*radius-(radius-cy-.5)**2)) if cy < radius else 0
            x = inset+math.ceil(corner)
            image.put(color, to=(x, y, 64-x, y+1))
    layer(border or fill, 0)
    if border:
        layer(fill, 2)
    return image


def apply_theme(root, font):
    style = ttk.Style(root)
    style.theme_use("clam")
    images = []
    for name, colors in (("TButton", ("#edf2f9", "#e3eafa", "#d7e2f5", INK)),
                         ("Accent.TButton", (BLUE, "#3263d8", "#2858c7", "#ffffff")),
                         ("Quiet.TButton", (CARD, "#eef3fb", "#e3ebf8", MUTED))):
        normal, hover, pressed, foreground = colors
        variants = [_surface(root, color) for color in (normal, hover, pressed, "#eef1f5")]
        variants.append(_surface(root, normal, BLUE if name != "Accent.TButton" else "#204cc0"))
        images.extend(variants)
        element = name.replace(".", "_")+".surface"
        style.element_create(element, "image", variants[0], ("disabled", variants[3]),
                             ("pressed", variants[2]), ("active", variants[1]),
                             ("focus", variants[4]), border=(12, 12), sticky="nsew")
        style.layout(name, [(element, {"sticky":"nsew", "children":[
            ("Button.padding", {"sticky":"nsew", "children":[("Button.label", {"sticky":"nsew"})]})]})])
        style.configure(name, padding=(13, 8), font=(font, 10), foreground=foreground,
                        background=BG, borderwidth=0, relief="flat", anchor="center")
        style.map(name, foreground=[("disabled", "#a5afbf")])
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=INK)
    style.configure("Card.TLabel", background=CARD, foreground=INK)
    style.configure("TCheckbutton", background=CARD, foreground=INK, padding=(0, 7), font=(font, 10))
    style.map("TCheckbutton", background=[("active", CARD)])
    style.configure("TEntry", padding=8, borderwidth=1, relief="flat", fieldbackground="#f7f9fc")
    style.configure("TCombobox", padding=7, borderwidth=0, relief="flat", arrowsize=12,
                    fieldbackground="#f1f5fa", background="#f1f5fa", foreground=INK,
                    bordercolor="#f1f5fa",lightcolor="#f1f5fa",darkcolor="#f1f5fa",arrowcolor=MUTED)
    style.map("TCombobox", fieldbackground=[("readonly", "#f1f5fa")], selectbackground=[("readonly", "#f1f5fa")],
              selectforeground=[("readonly", INK)])
    style.configure("TSpinbox", padding=6, borderwidth=0, arrowsize=10, fieldbackground="#f1f5fa")
    style.configure("TNotebook", background=CARD, borderwidth=0, tabmargins=(0,0,0,12),
                    bordercolor=CARD,lightcolor=CARD,darkcolor=CARD)
    tabs=[_surface(root,color) for color in (CARD,"#edf3ff","#f5f7fb")]
    images.extend(tabs)
    style.element_create("Modern.Notebook.tab","image",tabs[0],("selected",tabs[1]),
                         ("active",tabs[2]),border=(12,12),sticky="nsew")
    style.layout("TNotebook.Tab",[("Modern.Notebook.tab",{"sticky":"nsew","children":[
        ("Notebook.padding",{"sticky":"nsew","children":[("Notebook.label",{"sticky":"nsew"})]})]})])
    style.configure("TNotebook.Tab", padding=(16,9), borderwidth=0, background=CARD, foreground=MUTED)
    style.map("TNotebook.Tab", background=[("selected", "#edf3ff"), ("active", "#f5f7fb")],
              foreground=[("selected", BLUE)])
    style.configure("Treeview", rowheight=43, font=(font, 9), background=CARD,
                    fieldbackground=CARD, foreground=INK, borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", "#e9f0ff")], foreground=[("selected", BLUE)])
    style.configure("Treeview.Heading", font=(font, 9), background="#f7f9fc", foreground=MUTED,
                    borderwidth=0, padding=(4,8), relief="flat")
    track=tk.PhotoImage(master=root,width=6,height=32)
    track.put(CARD,to=(0,0,6,32))
    thumbs=[]
    for color in ("#d1dae5","#a7b7cc"):
        thumb=tk.PhotoImage(master=root,width=6,height=32)
        thumb.put(color,to=(1,0,5,32));thumb.put(color,to=(0,2,6,30));thumbs.append(thumb)
    images.extend([track,*thumbs])
    style.element_create("Modern.Scrollbar.trough","image",track,border=(0,0),sticky="ns")
    style.element_create("Modern.Scrollbar.thumb","image",thumbs[0],("active",thumbs[1]),
                         ("pressed",thumbs[1]),border=(2,3),sticky="nsew")
    style.layout("Vertical.TScrollbar", [("Modern.Scrollbar.trough", {"sticky":"ns", "children":[
        ("Modern.Scrollbar.thumb", {"sticky":"nswe"})]})])
    style.configure("Vertical.TScrollbar", width=6, gripcount=0, borderwidth=0,
                    background="#d1dae5", troughcolor=CARD, lightcolor="#d1dae5", darkcolor="#d1dae5")
    style.map("Vertical.TScrollbar", background=[("active", "#a7b7cc"), ("pressed", "#91a6c1")])
    return images


def rounded_rectangle(canvas, x, y, right, bottom, radius=10, **options):
    radius = min(radius, (right-x)/2, (bottom-y)/2)
    return canvas.create_polygon(x+radius,y, right-radius,y, right,y, right,y+radius,
                                 right,bottom-radius, right,bottom, right-radius,bottom,
                                 x+radius,bottom, x,bottom, x,bottom-radius, x,y+radius, x,y,
                                 smooth=True, splinesteps=24, **options)
