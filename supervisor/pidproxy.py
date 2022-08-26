#!/usr/bin/env python -u

"""pidproxy -- run command and proxy signals to it via its pidfile.

This executable runs a command and then monitors a pidfile.  When this
executable receives a signal, it sends the same signal to the pid
in the pidfile.

Usage: %s <pidfile name> <command> [<cmdarg1> ...]
"""

import os
import sys
import signal
import time

class PidProxy:
    pid = None

    def __init__(self, args):
        try:
            self.pidfile, cmdargs = args[1], args[2:]
            self.abscmd = os.path.abspath(cmdargs[0])
            self.cmdargs = cmdargs
        except (ValueError, IndexError):
            self.usage()
            # sys.exit([arg]), 0表示正常退出，在Unix程序中，
            # 2表示命令行语法错误，1表示所有其他类型的错误
            sys.exit(1)

    def go(self):
        self.setsignals()
        # os.spawnv()也是os模块提供的一个执行命令的功能函数，但是不同于os.system()
        # 只能执行shell命令，os.spawnv()可以执行更多“可执行”文件，包括C编译后的文件、
        # python可执行文件、shell命令等，帮助supervisor启动非python编写的服务
        # os.P_NOWAIT，返回进程号，如果是os.P_WAIT,返回进程退出的编码，若编码为负，表示出错
        self.pid = os.spawnv(os.P_NOWAIT, self.abscmd, self.cmdargs)
        while 1:
            time.sleep(5)
            try:
                pid = os.waitpid(-1, os.WNOHANG)[0]
            except OSError:
                pid = None
            if pid:
                break

    def usage(self):
        print(__doc__ % sys.argv[0])

    def setsignals(self):
        # signal.signal(信号类型, 执行信号类型的信号处理函数)
        # signal.SIGTERM，软件终止信号
        signal.signal(signal.SIGTERM, self.passtochild)
        # signal.SIGHUP,挂断信号，对于与终端脱离关系的守护进程，这个信号可以用于通知它重新读取配置文件
        signal.signal(signal.SIGHUP, self.passtochild)
        # signal.SIGINT，进程中断(INTERRUPT)信号,Ctrl + C
        signal.signal(signal.SIGINT, self.passtochild)
        # signal.SIGUSR1 & signal.SIGUSR2，都是留给用户使用的信号
        signal.signal(signal.SIGUSR1, self.passtochild)
        signal.signal(signal.SIGUSR2, self.passtochild)
        # signal.SIGQUIT，进程退出信号，CTRL + \
        signal.signal(signal.SIGQUIT, self.passtochild)
        # signal.SIGCHLD,在子进程结束时，父进程会收到这个信号，如果子进程没有处理这个信号，
        # 也没要等待子进程，子进程虽然终止，但是还是会中内核进程表中占有表项，成为僵尸进程
        signal.signal(signal.SIGCHLD, self.reap)

    def reap(self, sig, frame):
        # do nothing, we reap our child synchronously
        pass

    def passtochild(self, sig, frame):
        try:
            with open(self.pidfile, 'r') as f:
                pid = int(f.read().strip())
        except:
            print("Can't read child pidfile %s!" % self.pidfile)
            return
        os.kill(pid, sig)   # 将指定的信号发送到具有指定PID的进程，此方法不返回任何值
        if sig in [signal.SIGTERM, signal.SIGINT, signal.SIGQUIT]:
            sys.exit(0)

def main():
    pp = PidProxy(sys.argv)
    pp.go()

if __name__ == '__main__':
    main()
