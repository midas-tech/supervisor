#!/usr/bin/env python

"""supervisord -- run a set of applications as daemons.

Usage: %s [options]

Options:
-c/--configuration FILENAME -- configuration file path (searches if not given)
-n/--nodaemon -- run in the foreground (same as 'nodaemon=true' in config file)
-s/--silent -- no logs to stdout (maps to 'silent=true' in config file)
-h/--help -- print this usage message and exit
-v/--version -- print supervisord version number and exit
-u/--user USER -- run supervisord as this user (or numeric uid)
-m/--umask UMASK -- use this umask for daemon subprocess (default is 022)
-d/--directory DIRECTORY -- directory to chdir to when daemonized
-l/--logfile FILENAME -- use FILENAME as logfile path
-y/--logfile_maxbytes BYTES -- use BYTES to limit the max size of logfile
-z/--logfile_backups NUM -- number of backups to keep when max bytes reached
-e/--loglevel LEVEL -- use LEVEL as log level (debug,info,warn,error,critical)
-j/--pidfile FILENAME -- write a pid file for the daemon process to FILENAME
-i/--identifier STR -- identifier used for this instance of supervisord
-q/--childlogdir DIRECTORY -- the log directory for child process logs
-k/--nocleanup --  prevent the process from performing cleanup (removal of
                   old automatic child log files) at startup.
-a/--minfds NUM -- the minimum number of file descriptors for start success
-t/--strip_ansi -- strip ansi escape codes from process output
--minprocs NUM  -- the minimum number of processes available for start success
--profile_options OPTIONS -- run supervisord under profiler and output
                             results based on OPTIONS, which  is a comma-sep'd
                             list of 'cumulative', 'calls', and/or 'callers',
                             e.g. 'cumulative,callers')
"""

import os
import time
import signal

from supervisor.medusa import asyncore_25 as asyncore

from supervisor.compat import as_string
from supervisor.options import ServerOptions
from supervisor.options import decode_wait_status
from supervisor.options import signame
from supervisor import events
from supervisor.states import SupervisorStates
from supervisor.states import getProcessStateDescription

class Supervisor:
    stopping = False # set after we detect that we are handling a stop request
    lastshutdownreport = 0 # throttle for delayed process error reports at stop
    process_groups = None # map of process group name to process group object
    stop_groups = None # list used for priority ordered shutdown

    def __init__(self, options):    # 初始化
        self.options = options      # 配置
        self.process_groups = {}
        self.ticks = {}

    def main(self):
        if not self.options.first:
            # prevent crash on libdispatch-based systems, at least for the
            # first request
            self.options.cleanup_fds()

        self.options.set_uid_or_exit()

        if self.options.first:
            self.options.set_rlimits_or_exit()

        # this sets the options.logger object
        # delay logger instantiation until after setuid
        self.options.make_logger()

        if not self.options.nocleanup:
            # clean up old automatic logs
            self.options.clear_autochildlogdir()

        self.run()  # 运行

    def run(self):
        self.process_groups = {} # clear
        self.stop_groups = None # clear
        events.clear()
        try:
            # 根据配置进行添加process
            for config in self.options.process_group_configs:
                # 查看config在process_groups中是否已经存在
                # 若不存在加入其中，并唤醒等待ProcessGroupAddedEvent条件变量的进程
                self.add_process_group(config)
            # 打开http web服务器
            self.options.openhttpservers(self)
            # 设置注册的信号量，用于捕获信号
            self.options.setsignals()
            # 主进程是否成为守护进程
            if (not self.options.nodaemon) and self.options.first:
                self.options.daemonize()
            # writing pid file needs to come *after* daemonizing or pid
            # will be wrong
            self.options.write_pidfile()
            self.runforever()   # 运行异步IO服务器
        finally:
            # 在异常退出时，进行相应的清理工作
            self.options.cleanup()

    def diff_to_active(self):
        new = self.options.process_group_configs
        cur = [group.config for group in self.process_groups.values()]

        curdict = dict(zip([cfg.name for cfg in cur], cur))
        newdict = dict(zip([cfg.name for cfg in new], new))

        added   = [cand for cand in new if cand.name not in curdict]
        removed = [cand for cand in cur if cand.name not in newdict]

        changed = [cand for cand in new
                   if cand != curdict.get(cand.name, cand)]

        return added, changed, removed

    def add_process_group(self, config):
        name = config.name
        if name not in self.process_groups:
            config.after_setuid()
            # 根据初始化后的配置文件，生成相应的子进程实例
            self.process_groups[name] = config.make_group()
            # 添加事件通知
            events.notify(events.ProcessGroupAddedEvent(name))
            return True
        return False

    def remove_process_group(self, name):
        if self.process_groups[name].get_unstopped_processes():
            return False
        self.process_groups[name].before_remove()
        del self.process_groups[name]
        events.notify(events.ProcessGroupRemovedEvent(name))
        return True

    def get_process_map(self):
        process_map = {}
        for group in self.process_groups.values():
            process_map.update(group.get_dispatchers())
        return process_map

    def shutdown_report(self):
        unstopped = []

        for group in self.process_groups.values():
            unstopped.extend(group.get_unstopped_processes())

        if unstopped:
            # throttle 'waiting for x to die' reports
            now = time.time()
            if now > (self.lastshutdownreport + 3): # every 3 secs
                names = [ as_string(p.config.name) for p in unstopped ]
                namestr = ', '.join(names)
                self.options.logger.info('waiting for %s to die' % namestr)
                self.lastshutdownreport = now
                for proc in unstopped:
                    state = getProcessStateDescription(proc.get_state())
                    self.options.logger.blather(
                        '%s state: %s' % (proc.config.name, state))
        return unstopped

    def ordered_stop_groups_phase_1(self):
        if self.stop_groups:
            # stop the last group (the one with the "highest" priority)
            self.stop_groups[-1].stop_all()

    def ordered_stop_groups_phase_2(self):
        # after phase 1 we've transitioned and reaped, let's see if we
        # can remove the group we stopped from the stop_groups queue.
        if self.stop_groups:
            # pop the last group (the one with the "highest" priority)
            group = self.stop_groups.pop()
            if group.get_unstopped_processes():
                # if any processes in the group aren't yet in a
                # stopped state, we're not yet done shutting this
                # group down, so push it back on to the end of the
                # stop group queue
                self.stop_groups.append(group)

    def runforever(self):
        # 事件通知机制
        events.notify(events.SupervisorRunningEvent())
        timeout = 1 # this cannot be fewer than the smallest TickEvent (5)

        # 获取已经注册的句柄
        socket_map = self.options.get_socket_map()

        # 这里会一直运行，相当于守护进程
        while 1:
            # 保存运行信息等到combined_map
            combined_map = {}
            combined_map.update(socket_map)  # 将socket_map项目插入到字典combined_map中
            combined_map.update(self.get_process_map()) # 将process_map也插入到字典combined_map中

            # 进程信息
            # values()方法提供的是实体字典的动态视图，字典发生变化时，视图也会跟着变化
            # 视图对象不是列表，不支持索引，可以使用list来转换为列表
            # 我们不能对视图对象进行任何的修改，因为视图对象是只读的
            pgroups = list(self.process_groups.values())
            pgroups.sort()

            # 根据进程配置开启或关闭进程
            # self.options.mood < 1时，表示主进程状态并非运行状态
            # 可能是重启、停止执行或出错状态，这时本进程也需要停止执行
            if self.options.mood < SupervisorStates.RUNNING:
                if not self.stopping:
                    # first time, set the stopping flag, do a
                    # notification and set stop_groups
                    self.stopping = True
                    self.stop_groups = pgroups[:]
                    events.notify(events.SupervisorStoppingEvent())

                # 启动：优先级从低到高顺序  停止：优先级从高到低顺序
                self.ordered_stop_groups_phase_1()

                if not self.shutdown_report():
                    # if there are no unstopped processes (we're done
                    # killing everything), it's OK to shutdown or reload
                    raise asyncore.ExitNow

            # 网络socket， io操作
            for fd, dispatcher in combined_map.items():
                if dispatcher.readable():
                    self.options.poller.register_readable(fd)
                if dispatcher.writable():
                    self.options.poller.register_writable(fd)

            # poll, io多路复用，如果返回为空的列表，则说明已超时且没有文件描述符有事件发生
            # 如果timeout为None，将会阻塞，直到有事件发生
            r, w = self.options.poller.poll(timeout)

            # 依次遍历注册的文件句柄
            for fd in r:        # 处理客户端的rpc读事件
                if fd in combined_map:
                    try:
                        dispatcher = combined_map[fd]
                        self.options.logger.blather(
                            'read event caused by %(dispatcher)r',
                            dispatcher=dispatcher)
                        dispatcher.handle_read_event()
                        if not dispatcher.readable():
                            self.options.poller.unregister_readable(fd)
                    except asyncore.ExitNow:
                        raise
                    except:
                        combined_map[fd].handle_error()

            for fd in w:       # 处理客户端的rpc写事件
                if fd in combined_map:  
                    try:
                        dispatcher = combined_map[fd]
                        self.options.logger.blather(
                            'write event caused by %(dispatcher)r',
                            dispatcher=dispatcher)
                        dispatcher.handle_write_event()
                        if not dispatcher.writable():
                            self.options.poller.unregister_writable(fd)
                    except asyncore.ExitNow:
                        raise
                    except:
                        combined_map[fd].handle_error()

            for group in pgroups:
                group.transition()

            self.reap()          # 获取已经死亡的子进程信息(默认最多不超过100条)
            self.handle_signal() # 处理信号
            self.tick()          # 发送一个或多个tick时钟事件

            if self.options.mood < SupervisorStates.RUNNING:
                self.ordered_stop_groups_phase_2()

            if self.options.test:
                break

    def tick(self, now=None):
        """ Send one or more 'tick' events when the timeslice related to
        the period for the event type rolls over """
        if now is None:
            # now won't be None in unit tests
            now = time.time()
        for event in events.TICK_EVENTS:
            period = event.period
            last_tick = self.ticks.get(period)
            if last_tick is None:
                # we just started up
                last_tick = self.ticks[period] = timeslice(period, now)
            this_tick = timeslice(period, now)
            if this_tick != last_tick:
                self.ticks[period] = this_tick
                events.notify(event(this_tick, self))

    def reap(self, once=False, recursionguard=0):
        if recursionguard == 100:
            return
        pid, sts = self.options.waitpid()
        if pid:
            process = self.options.pidhistory.get(pid, None)
            if process is None:
                _, msg = decode_wait_status(sts)
                self.options.logger.info('reaped unknown pid %s (%s)' % (pid, msg))
            else:
                process.finish(pid, sts)
                del self.options.pidhistory[pid]
            if not once:
                # keep reaping until no more kids to reap, but don't recurse
                # infintely
                self.reap(once=False, recursionguard=recursionguard+1)

    def handle_signal(self):
        sig = self.options.get_signal()
        if sig:
            if sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
                self.options.logger.warn(
                    'received %s indicating exit request' % signame(sig))
                self.options.mood = SupervisorStates.SHUTDOWN
            elif sig == signal.SIGHUP:
                if self.options.mood == SupervisorStates.SHUTDOWN:
                    self.options.logger.warn(
                        'ignored %s indicating restart request (shutdown in progress)' % signame(sig))
                else:
                    self.options.logger.warn(
                        'received %s indicating restart request' % signame(sig))
                    self.options.mood = SupervisorStates.RESTARTING
            elif sig == signal.SIGCHLD:
                self.options.logger.debug(
                    'received %s indicating a child quit' % signame(sig))
            elif sig == signal.SIGUSR2:
                self.options.logger.info(
                    'received %s indicating log reopen request' % signame(sig))
                self.options.reopenlogs()
                for group in self.process_groups.values():
                    group.reopenlogs()
            else:
                self.options.logger.blather(
                    'received %s indicating nothing' % signame(sig))

    def get_state(self):
        return self.options.mood

def timeslice(period, when):
    return int(when - (when % period))

# profile entry point
def profile(cmd, globals, locals, sort_order, callers): # pragma: no cover
    try:
        import cProfile as profile
    except ImportError:
        import profile
    import pstats
    import tempfile
    fd, fn = tempfile.mkstemp()
    try:
        profile.runctx(cmd, globals, locals, fn)
        stats = pstats.Stats(fn)
        stats.strip_dirs()
        # calls,time,cumulative and cumulative,calls,time are useful
        stats.sort_stats(*sort_order or ('cumulative', 'calls', 'time'))
        if callers:
            stats.print_callers(.3)
        else:
            stats.print_stats(.3)
    finally:
        os.remove(fn)


# Main program
def main(args=None, test=False):
    assert os.name == "posix", "This code makes Unix-specific assumptions"
    # if we hup, restart by making a new Supervisor()
    first = True
    while 1:
        # ServerOptions:配置文件的优化，提供服务器的初始化和主进程变为守护进程
        # 子进程的创建和管理工作
        options = ServerOptions() # 配置
        options.realize(args, doc=__doc__)
        options.first = first
        options.test = test
        if options.profile_options:
            sort_order, callers = options.profile_options
            profile('go(options)', globals(), locals(), sort_order, callers)
        else:
            go(options)     # 加载配置，开始运行
        options.close_httpservers()
        options.close_logger()
        first = False
        if test or (options.mood < SupervisorStates.RESTARTING):
            break

def go(options): # pragma: no cover
    # 基于异步的IO服务器运行
    # 接受子进程、RPC客户端的通信处理等工作
    d = Supervisor(options)     # 利用options实例化一个Supervisor对象
    try:
        # 运行时参数日志等的初始化，生成子进程，打开http服务器等工作
        d.main()
    except asyncore.ExitNow:
        pass

if __name__ == "__main__": # pragma: no cover
    main()
