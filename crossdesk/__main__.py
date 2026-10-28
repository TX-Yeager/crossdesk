import argparse
from pathlib import Path
import sys
import signal
import threading


def main():
    parser=argparse.ArgumentParser(description="CrossDesk · Windows / Mac 双向跨屏")
    parser.add_argument("--state-dir",type=Path,help="独立配置目录（默认当前用户应用数据目录）")
    parser.add_argument("--demo",action="store_true",help="三屏画布演示，不连接网络、不捕获输入")
    parser.add_argument("--smoke-test",action="store_true",help="以演示后端验证GUI启动后自动退出")
    args=parser.parse_args()
    import tkinter as tk
    from tkinter import messagebox
    from crossdesk.app import App,DemoBackend,default_state_dir
    from crossdesk.backends import native_backend
    root=None
    app=None
    smoke_failed=[]
    failure=None
    exit_code=0
    previous_sigterm=None
    signal_registered=False
    signal_queued=False
    try:
        root=tk.Tk()
        demo=args.demo or args.smoke_test
        app=App(root,DemoBackend() if demo else native_backend(),args.state_dir or default_state_dir(),demo)
        if not demo and threading.current_thread() is threading.main_thread():
            def sigterm_handler(_signum,_frame):
                nonlocal signal_queued
                if signal_queued:
                    return
                signal_queued=True
                try:
                    root.after(0,app.close)
                except tk.TclError:
                    pass
            previous_sigterm=signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM,sigterm_handler)
            signal_registered=True
        if args.smoke_test:
            def smoke():
                try:
                    assert len(app.layout)==3
                    assert app.canvas.find_all()
                    assert not app.engine.enabled
                except Exception as exc:
                    smoke_failed.append(exc)
                finally:
                    if not app.close():
                        smoke_failed.append(app.close_error)
            root.after(400,smoke)
        root.mainloop()
    except KeyboardInterrupt:
        exit_code=130
    except Exception as exc:
        failure=exc
        exit_code=1
    finally:
        try:
            if app is not None:
                if not app.close() and exit_code==0:
                    exit_code=1
            elif root is not None:
                root.destroy()
        except Exception as exc:
            failure=failure or exc
            exit_code=1
        finally:
            if signal_registered:
                signal.signal(signal.SIGTERM,previous_sigterm)
    if failure is not None:
        if args.smoke_test or root is None:
            if sys.stderr is not None:
                print(f"CrossDesk startup failed: {type(failure).__name__}: {failure}",file=sys.stderr)
        else:
            messagebox.showerror("CrossDesk 启动失败",str(failure))
    return exit_code or (1 if smoke_failed else 0)


if __name__=="__main__":
    sys.exit(main())
