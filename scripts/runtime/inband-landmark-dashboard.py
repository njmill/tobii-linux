#!/usr/bin/env python3
"""Passive camera/landmark view of the tracking service; performs no inference."""
import argparse
import base64
from pathlib import Path
import signal
import tkinter as tk

from service_protocol import ServiceClient, default_socket


class LandmarkView:
    def __init__(self, root, path):
        self.root = root
        self.root.configure(background='#090d12')
        self.root.attributes('-topmost', True)
        # KWin stacks managed keep-above windows below an active fullscreen game.
        # An unmanaged X11/XWayland overlay occupies the compositor's overlay layer.
        self.overlay = self.root.tk.call('tk', 'windowingsystem') == 'x11'
        if self.overlay:
            self.root.overrideredirect(True)
        self.root.minsize(280, 200)
        self.client = ServiceClient(path, preview=True)
        self.show_camera = tk.BooleanVar(root, value=True)
        self.canvas = tk.Canvas(root, background='#090d12', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.camera_check = tk.Checkbutton(
            self.canvas, text='Show camera', variable=self.show_camera,
            background='#090d12', foreground='#dbe7f3',
            activebackground='#090d12', activeforeground='#dbe7f3',
            selectcolor='#18212b', highlightthickness=0, borderwidth=0,
            font=('Sans', 10), cursor='hand2')
        self.camera_check.place(relx=1, x=-42 if self.overlay else -12, y=12, anchor=tk.NE)
        if self.overlay:
            self.close_button = tk.Button(
                self.canvas, text='×', command=self.close, font=('Sans', 13),
                background='#090d12', foreground='#91a0b2',
                activebackground='#18212b', activeforeground='#dbe7f3',
                borderwidth=0, highlightthickness=0, cursor='hand2', takefocus=False)
            self.close_button.place(relx=1, x=-10, y=9, anchor=tk.NE)
            self.canvas.bind('<ButtonPress-1>', self.begin_move)
            self.canvas.bind('<B1-Motion>', self.move)
            self.resize_grip = tk.Label(
                self.canvas, text='◢', background='#090d12', foreground='#91a0b2',
                cursor='bottom_right_corner', font=('Sans', 12))
            self.resize_grip.place(relx=1, rely=1, x=-4, y=-4, anchor=tk.SE)
            self.resize_grip.bind('<ButtonPress-1>', self.begin_resize)
            self.resize_grip.bind('<B1-Motion>', self.resize)
        self.move_origin = None
        self.resize_origin = None
        self.image = None
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Escape>', self.close)
        self.root.bind('q', self.close)
        self.root.after(50, self.tick)

    def begin_move(self, event):
        self.move_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y()) if event.y < 45 else None

    def move(self, event):
        if self.move_origin is not None:
            x, y, left, top = self.move_origin
            self.root.geometry(f'{left + event.x_root - x:+d}{top + event.y_root - y:+d}')

    def begin_resize(self, event):
        self.resize_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def resize(self, event):
        if self.resize_origin is not None:
            x, y, width, height = self.resize_origin
            self.root.geometry(f'{max(280, width + event.x_root - x)}x{max(200, height + event.y_root - y)}')

    def tick(self):
        snapshot, connected, error = self.client.read()
        canvas = self.canvas
        canvas.delete('all')
        canvas.create_text(20, 16, anchor='nw', fill='#dbe7f3', font=('Sans',12,'bold'), text='Landmarks')
        preview = snapshot.get('preview') if snapshot is not None and connected else None
        status = snapshot['status'] if connected and snapshot is not None else 'Service disconnected: '+error
        if preview and preview['pgm']:
            try:
                image = tk.PhotoImage(data=base64.b64decode(preview['pgm']), format='PPM')
                scale = max(1,min(3,int((canvas.winfo_width()-40)/max(image.width(),1)),
                    int((canvas.winfo_height()-120)/max(image.height(),1))))
                self.image = image.zoom(scale,scale) if self.show_camera.get() else None
                if self.show_camera.get():
                    canvas.create_image(20,55,anchor='nw',image=self.image)
                for point in preview['landmarks']:
                    x,y=20+point['x']*scale,55+point['y']*scale
                    canvas.create_oval(x-1,y-1,x+1,y+1,fill='#46d97d',outline='')
            except (ValueError,tk.TclError) as exc:
                status = f'Preview unavailable: {exc}'
        canvas.create_text(20,canvas.winfo_height()-40,anchor='nw',fill='#91a0b2',font=('Sans',11),text=status)
        self.root.after(50,self.tick)

    def close(self, _event=None):
        self.client.close()
        self.root.destroy()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket',type=Path,default=default_socket())
    args=parser.parse_args()
    root=tk.Tk();root.title('Tobii service landmark view');root.geometry('880x940')
    view=LandmarkView(root,args.socket)
    signal.signal(signal.SIGTERM,lambda *_:root.after(0,view.close))
    try:root.mainloop()
    finally:view.client.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
