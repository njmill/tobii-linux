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
        self.client = ServiceClient(path, preview=True)
        self.canvas = tk.Canvas(root, background='#090d12', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.image = None
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Escape>', self.close)
        self.root.bind('q', self.close)
        self.root.after(50, self.tick)

    def tick(self):
        snapshot, connected, error = self.client.read()
        canvas = self.canvas
        canvas.delete('all')
        title = 'Service camera and MediaPipe landmarks'
        canvas.create_text(20, 16, anchor='nw', fill='#dbe7f3', font=('Sans',14,'bold'), text=title)
        preview = snapshot.get('preview') if snapshot is not None and connected else None
        status = snapshot['status'] if connected and snapshot is not None else 'Service disconnected: '+error
        if preview and preview['pgm']:
            try:
                image = tk.PhotoImage(data=base64.b64decode(preview['pgm']), format='PPM')
                scale = max(1,min(3,int((canvas.winfo_width()-40)/max(image.width(),1)),
                    int((canvas.winfo_height()-120)/max(image.height(),1))))
                self.image = image.zoom(scale,scale)
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
