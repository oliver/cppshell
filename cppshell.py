#!/usr/bin/python

#
# Interactive C++ shell - you type C++ code, and it's compiled and executed on the fly in the background.
#


import sys
import os
import re
import tempfile
import subprocess
import fcntl
import gobject
import gtk
import gtk.glade
import pango


class Compiler:
    def __init__ (self, text, onFinishedCb):
        self.onFinishedCb = onFinishedCb

        (fileno, self.tempFile) = tempfile.mkstemp(prefix='cpp-', suffix='.C', text=True)
        fd = os.fdopen(fileno, 'w')
        fd.write(text)
        fd.close()

        self.exePath = tempfile.mktemp(prefix='cpp-', suffix='.bin')

        cmd = ['g++', '-W', '-Wall', '-Wextra', self.tempFile, '-o', self.exePath]
        print cmd

        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gobject.io_add_watch(self.proc.stdout, gobject.IO_IN | gobject.IO_ERR | gobject.IO_HUP,
            self._onReadable)
        gobject.io_add_watch(self.proc.stderr, gobject.IO_IN | gobject.IO_ERR | gobject.IO_HUP,
            self._onReadable)
        self.pipesOpen = 2

    def _onReadable (self, fd, cond):
        if (cond & gobject.IO_IN):
            readText = fd.read(4000)
            print "(read %d bytes)" % len(readText)
            return True
        else:
            # read all remaining data from pipe
            while True:
                readText = fd.read(4000)
                print "(read %d bytes before finish)" % len(readText)
                if len(readText) <= 0:
                    break

            result = fd.close()
            if result == None:
                print "(command finished successfully)"
                pass
            else:
                print "(command finished with exit code %d; exited: %s, exit status: %d)" % (result,
                    str(os.WIFEXITED(result)), os.WEXITSTATUS(result))
                pass
            self.proc.wait()

            self.pipesOpen -= 1
            print "now have %d pipes open" % self.pipesOpen
            if self.pipesOpen <= 0:
                os.unlink(self.tempFile)
                self.onFinishedCb(self.exePath)
            return False


class Executer:
    def __init__ (self, command, finishedCb, outputCb):
        self.finishedCb = finishedCb
        self.outputCb = outputCb

        cmd = [command]
        print cmd

        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # make pipes non-blocking:
        fl = fcntl.fcntl(self.proc.stdout, fcntl.F_GETFL)
        fcntl.fcntl(self.proc.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        fl = fcntl.fcntl(self.proc.stderr, fcntl.F_GETFL)
        fcntl.fcntl(self.proc.stderr, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        gobject.io_add_watch(self.proc.stdout, gobject.IO_IN | gobject.IO_ERR | gobject.IO_HUP,
            self._onReadable)
        gobject.io_add_watch(self.proc.stderr, gobject.IO_IN | gobject.IO_ERR | gobject.IO_HUP,
            self._onReadable)
        self.pipesOpen = 2

    def _onReadable (self, fd, cond):
        if (cond & gobject.IO_IN):
            readText = fd.read(4000)
            self.outputCb(readText)
            print "(read %d bytes)" % len(readText)
            return True
        else:
            # read all remaining data from pipe
            while True:
                readText = fd.read(4000)
                print "(read %d bytes before finish)" % len(readText)
                if len(readText) <= 0:
                    break
                self.outputCb(readText)

            result = fd.close()
            if result == None:
                print "(command finished successfully)"
                pass
            else:
                print "(command finished with exit code %d; exited: %s, exit status: %d)" % (result,
                    str(os.WIFEXITED(result)), os.WEXITSTATUS(result))
                pass
            self.proc.wait()

            self.pipesOpen -= 1
            print "now have %d pipes open" % self.pipesOpen
            if self.pipesOpen <= 0:
                self.finishedCb()
            return False


(STATE_INITIAL, STATE_COMPILING, STATE_RUNNING, STATE_FINISHED) = range(0, 4)


class Task:
    def __init__ (self, inText, onOutput):
        self.inputText = inText
        self.exePath = None
        self.outputText = None
        self.onOutput = onOutput

    def start (self, taskFinishedCb):
        self.taskFinishedCb = taskFinishedCb
        self.work()

    def work (self):
        if self.exePath is None:
            self.compiler = Compiler(self.inputText, self._onCompileFinished)
        elif self.outputText is None:
            self.executer = Executer(self.exePath, self._onExecFinished, self.onOutput)
        else:
            # should not happen
            assert(False)

    def _onCompileFinished (self, exePath):
        print "compile finished; exe: '%s'" % exePath
        self.exePath = exePath
        self.compiler = None
        self.work()

    def _onExecFinished (self):
        print "execution finished"
        self.outputText = "abc"
        self.taskFinishedCb()

#    def _onOutput (self, line):
#        print "O: " + line


class ExecQueue:
    "holds the next state that has to be compiled and executed"
    def __init__ (self):
        self.tActive = None
        self.tQueued = None

    def enqueue (self, task):
        if self.tActive is None:
            self.tActive = task
            self.startWorking()
        else:
            # replace existing queued task
            self.tQueued = task

    def startWorking (self):
        # start working on self.tActive
        self.tActive.start(self._onTaskFinished)

    def _onTaskFinished (self):
        print "task finished"
        self.tActive = None
        if self.tQueued is not None:
            self.tActive = self.tQueued
            self.tQueued = None
            self.startWorking()




cppTemplate = """

%s

#include <iostream>
using namespace std;

int main (int argc, char* argv[])
{

%s

}
"""


class CppShellGui:
    def __init__ (self):
        gladeFile = os.path.join(os.path.realpath( os.path.dirname(sys.argv[0]) ), 'cppshell.glade')
        self.tree = gtk.glade.XML(gladeFile, 'winMain')
        self.tree.signal_autoconnect(self)

        self.txtIn = self.tree.get_widget('txtInput')
        self.bufferIn = gtk.TextBuffer()
        self.txtIn.set_buffer(self.bufferIn)

        self.txtOut = self.tree.get_widget('txtOutput')
        self.bufferOut = gtk.TextBuffer()
        self.txtOut.set_buffer(self.bufferOut)

        pangoFont = pango.FontDescription("Monospace")
        self.txtIn.modify_font(pangoFont)
        self.txtOut.modify_font(pangoFont)

        #self.bufferIn.set_text("""cout << "test" << endl;\n""")

        self.queue = ExecQueue()

        self.saveFileName = os.path.expanduser('~/.config/cppshell.txt')
        try:
            fd = open(self.saveFileName, 'r')
            text = fd.read()
            fd.close()
            self.bufferIn.set_text(text)
        except:
            pass

    def on_winMain_delete_event (self, widget, dummy):
        gtk.main_quit()

    def on_tbExecute_clicked (self, button):
        print "executing..."
        text = self.bufferIn.get_text(self.bufferIn.get_start_iter(), self.bufferIn.get_end_iter())
        print text

        task = self._makeTask()
        self.queue.enqueue(task)

        fd = open(self.saveFileName, 'w')
        fd.write(text)
        fd.close()

    def _makeTask (self):
        userText = self.bufferIn.get_text(self.bufferIn.get_start_iter(), self.bufferIn.get_end_iter())

        includeLines = ""
        codeLines = ""
        for line in userText.splitlines(True):
            if re.search('^\s#include', line):
                includeLines += line
            else:
                codeLines += line
        
        cppText = cppTemplate % (includeLines, codeLines)
        return Task(cppText, self.onOutput)

    def onOutput (self, text):
        self.bufferOut.insert(self.bufferOut.get_end_iter(), text)

if __name__ == '__main__':
    gui = CppShellGui()
    gtk.main()

