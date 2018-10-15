# -*- coding: utf-8 -*-
# Copyright: (C) 2018 Lovac42
# Support: https://github.com/lovac42/SM2-Emulator
# License: GNU GPL, version 3 or later; http://www.gnu.org/copyleft/gpl.html
# Version: 0.0.1


from __future__ import division
from aqt import mw
from anki.hooks import wrap, addHook
from aqt.reviewer import Reviewer
from anki.sched import Scheduler
from anki.utils import intTime, fmtTimeSpan, ids2str
# from aqt.utils import showWarning, showText
from heapq import *
import time, random


# CONFIG ###################################

#ADDS SLIGHT INTERLEAVE
DELAY_AGAINED  = 0 # this + learning steps[0]
DELAY_HARD     = 30 # this + learning steps[0]

#FACTOR ADD/SUB
INC_FACTOR = 100  #EasyBtn: 100 sm2, 150 anki
DEC_FACTOR = -140  #HardBtn: -140 sm2, -150 anki
ALT_FACTOR = 0  #AgainBtn: 0 sm2, -200 anki, -160 Mnemosyne

# END_CONFIG ###########################################


#Initial Intervals
INIT_IVL=1
SEC_IVL =6    #anki: 1*EF or about 2-3 days after fuzz
BUMP_IVL=21   #Breaks out of low interval hell

# idx:[ display_name, dynamic, initial, secondary]
PRIORITY_LEVELS = {
  0:["Normal (SM2)",    False, 1,  6], #sm2 default
  1:["Slacker",         False, 3,  7],
  2:["Vacation",        False, 5, 14],
  3:["Beefcake (Anki)", False, 1,  3], #similar to anki's default config
  4:["Auto Defer Leech", True, 4, 10], #1:6 for new, 4:10 max for leech cards
}



#####################################################################
####   Filters, don't apply addon to certain models  ################
#####################################################################
isFilteredCard = False

def isFiltered():
    if mw.col.sched.name=="std2":
        return True

    did = mw.col.decks.selected()
    if mw.col.decks.get(did)['dyn']:
        return True

    card = mw.reviewer.card
    conf = mw.col.decks.confForDid(card.did)
    if not conf.get("sm2emu", False):
        return True

    model = card.model()['name']
    if model=='IR3' or model[:6]=='IRead2': #Avoid IR Cards
        return True

    return False

def onShowQuestion():
    global isFilteredCard
    isFilteredCard=isFiltered()
addHook('showQuestion', onShowQuestion)

#####################################################################
####      Button Display                         ####################
#####################################################################

def answerButtons(self, card, _old):
    if isFilteredCard:
        return _old(self, card)
    return 4

def answerButtonList(self, _old):
    if isFilteredCard:
        return _old(self)

    #Learning card
    if self.card.queue==1:
        return ((1, _('<font color="maroon">Bust</font>')), 
                (2, _('<font color="black">Hit</font>')),
                (3, _('<font color="navy">Stay</font>')), 
                (4, _('<font color="green">BJ</font>'))  )

    #New or review card
    return ((1, _('<font color="maroon">Dunno</font>')), 
            (2, _('<font color="black">Difficult</font>')),
            (3, _('<font color="navy">Hesitated</font>')), 
            (4, _('<font color="green">Pefecto</font>'))  )


def buttonTime(self, i, _old):
    c=card=self.card
    if isFilteredCard:
        return _old(self, i)

    text=None
    if i==1:
        text='IVL 0' if c.ivl<21 else 'Revert'
        return '<font color="pink" class="nobold">%s</font><br>'%text

    elif i==2:
        text='%.1f EF'%(adjustFactor(c,DEC_FACTOR)/1000.0)
        return '<font color="gray" class="nobold">%s</font><br>'%text

    elif i==3:
        extra='1d, ' if c.queue==1 and c.ivl>=21 else ''
        text=nextIntervalString(c, i)
        return '<font color="aqua" class="nobold">%s%s</font><br>'%(extra,text)

    elif i==4:
        if c.queue!=1 and c.ivl<=INIT_IVL:
            text='%dd Bump'%BUMP_IVL
        else:
            # factor=getEaseFactor(c,i)
            # text=nextIntervalString(c, i)
            # text+=' %.1f OF'%(factor)
            factor=adjustFactor(c,INC_FACTOR)
            text='%.1f EF'%max(1.4,factor/1000.0)
        return '<font color="lime" class="nobold">%s</font><br>'%text

    return '!err'


#####################################################################
########   Custom Scheduler                              ############
#####################################################################

#TYPE-QUEUE FLAGS:
# 00 = new cards
# 01 = new learning cards
# 21 = lapsed relearning cards
# 22 = reviews

LOG_REVIEWED=1
LOG_LEARNED=0


def answerCard(self, card, ease, _old):
    if isFilteredCard:
        return _old(self, card, ease)

    self.col.log()
    assert ease >= 1 and ease <= 4
    self.col.markReview(card) #for undo
    if self._burySiblingsOnAnswer:
        self._burySiblings(card)

    #SETUP LOGGING PARAMS
    revType = 'rev'
    logType = LOG_REVIEWED
    card.lastIvl = card.ivl
    if card.type==0 and card.queue==0: #new card
        logType = LOG_LEARNED
        revType = 'new'
    elif card.queue==1:
        logType = card.type # re/learning card
        revType = 'lrn'

    #PROCESS GRADES
    if ease==1: #reset young, revert matured
        if not isLeechCard(card): #chk suspend
            card.ivl=revertInterval(card)
            repeatCard(self, card, DELAY_AGAINED) #sets queue to 1
            card.factor=adjustFactor(card, ALT_FACTOR)

    elif ease==2: #repeat, -140ef
        card.factor=adjustFactor(card, DEC_FACTOR)
        repeatCard(self, card, DELAY_HARD) #sets queue to 1

    elif ease<=4: #advance
        #Repeats an extra day to avoid judgement of learning bias (not in SM2)
        if card.queue==1 and card.ivl>=21:
            card.due = self.today + 1
        else:
            idealIvl = nextInterval(self, card, ease)
            card.ivl = custFuzzedIvl(self.today, idealIvl)
            card.due = self.today + idealIvl
        card.type = card.queue = 2
        card.left = 0
        if ease==4: #Mnemosyne adds this value first, anki adds this last, makes little diff to IVL
            card.factor=adjustFactor(card, INC_FACTOR)

    #LOG THIS REVIEW
    logStats(card, ease, logType)
    self._updateStats(card, revType)
    self._updateStats(card, 'time', card.timeTaken())
    card.reps += 1
    card.mod = intTime()
    card.usn = self.col.usn()
    card.flushSched()


def adjustFactor(card, n):
    fct=2500 if card.factor==0 else card.factor
    fct += n
    return max(fct,1300)


#Trim EF based on number of lapses
def getEaseFactor(card, ease=3, delay=0):
    if card.reps==0: #prevent div by 0 on new cards
        return 2.5
    fct=adjustFactor(card, -delay)
    lr=card.lapses/card.reps #Leech Ratio
    if ease==4 and card.queue!=1:
        if card.ivl>21:
            fct=max(1.2, fct * (1.05-lr) / 1000.0)
        else:
            fct=max(1.3, fct * (1.15-lr) / 1000.0)
    else: #ease3
        fct=max(1.2, fct * (1-lr) / 1000.0)
    return min(4, fct) #TODO: find max optimal value


def adjustPriorityInterval(card, conf):
    global INIT_IVL, SEC_IVL
    level=conf.get("sm2priority", 0)
    deferLeech=PRIORITY_LEVELS[level][1]
    INIT_IVL=PRIORITY_LEVELS[level][2]
    SEC_IVL=PRIORITY_LEVELS[level][3]
    return deferLeech #bool


def nextIntervalString(card, ease): #button date display
    ivl=nextInterval(mw.col.sched, card, ease)
    #displays exact fuzzed date, but maybe process intensive
    # if ivl<33: ivl=custFuzzedIvl(mw.col.sched.today, ivl)
    return fmtTimeSpan(ivl*86400, short=True)


def nextInterval(self, card, ease):
    if ease==4 and card.queue!=1 and card.ivl<=INIT_IVL:
        return BUMP_IVL

    conf=mw.col.decks.confForDid(card.did)
    deferLeech=adjustPriorityInterval(card, conf)
    modifier=conf['rev'].get('ivlFct', 1)
    idealIvl=1

    if card.ivl==0:
        idealIvl=INIT_IVL
        if deferLeech:
            ef=getEaseFactor(card, ease)
            idealIvl -= (ef-1.3)*3/1.2
    elif card.ivl<SEC_IVL:
        idealIvl=SEC_IVL
        if deferLeech:
            ef=getEaseFactor(card, ease)
            idealIvl -= (ef-1.3)*INIT_IVL/1.2
    else:
        delay = 0
        if card.queue!=1 and card.ivl>=21:
            delay = max(-10, self.today - card.due) #slight punishment for reviewing ahead.
            delay = min(card.ivl, min(100, delay)) #paused young decks
        ef=getEaseFactor(card, ease, delay)
        idealIvl = (card.ivl + delay // 2) * ef * modifier

    return min(int(idealIvl), conf['rev']['maxIvl'])


#REPLACE RANDOMIZED DATES WITH LOAD BALANCING.
#Some codes came from anki.sched.Scheduler.dueForecast.
def custFuzzedIvl(today, ivl):
    if ivl <= SEC_IVL: return ivl
    minDay, maxDay = custFuzzIvlRange(ivl)
    if minDay<90:
        #In cases of paused decks, balancing per deck is preferred.
        #But not in cases where there are too many sub-decks.
        perDeck=""
        if maxDay>32:
            perDeck="did in %s and"%ids2str(mw.col.decks.active())

        daysd = dict(mw.col.db.all("""
select due, count() from cards
where %s queue = 2
and due between ? and ?
group by due
order by due"""%perDeck,
        today+minDay, today+maxDay))

        if daysd:
            for d in range(minDay,maxDay):
                d = today+d
                if d not in daysd:
                    daysd[d] = 0
            idealDay=min(daysd, key=daysd.get)
            return idealDay - today
    return random.randint(minDay, maxDay)


def custFuzzIvlRange(ivl):
    if ivl < 15: return [ivl, ivl+1]    #2
    if ivl < 21: return [ivl-1, ivl+1]  #3
    if ivl < 42: return [ivl-1, ivl+2]  #4
    if ivl < 60: return [ivl-2, ivl+2]  #5
    if ivl < 120: return [ivl-2, ivl+3] #6
    return [ivl-3, ivl+3] #max 7 range


#####################################################################
#######          Utils                                ##############
#####################################################################


#log type
#0 = learned
#1 = review
#2 = relearned
#3 = filtered, not used here

def logStats(card, ease, type): #copied & modded from anki.sched.logStats
    def log():
        mw.col.db.execute(
            "insert into revlog values (?,?,?,?,?,?,?,?,?)",
            int(time.time()*1000), card.id, mw.col.usn(), ease,
            card.ivl, card.lastIvl, card.factor, card.timeTaken(), type)
    try:
        log()
    except:
        time.sleep(0.01) # duplicate pk; retry in 10ms
        log()


def isLeechCard(card): #review cards only
    if card.queue!=2: return False
    card.lapses += 1
    conf=mw.col.sched._lapseConf(card)
    return mw.col.sched._checkLeech(card,conf)


def revertInterval(card): #Inspired by the addon "Another Retreat"
    # return 0 #default sm2 behavior
    if card.ivl < 21: return 0
    hist = mw.col.db.list("""
select ivl from revlog where cid = ? and ivl >= 21 order by id desc
""", card.id)
    if hist:
        hist = [i for i in hist if i < card.ivl]
        if hist: return hist[0]
    return 0


def repeatCard(self, card, due):
    card.left = 1001
    conf=self._lrnConf(card)
    delay=self._delayForGrade(conf,0)
    card.queue = 1
    card.due = intTime() + delay + due
    self.lrnCount += 1
    heappush(self._lrnQueue, (card.due, card.id))


#Randomize learning stack
def fillLrn(self, _old):
    if mw.col.sched.name=="std2":
        return _old(self)

    did = mw.col.conf['curDeck']
    conf = mw.col.decks.confForDid(did)
    if conf.get("sm2emu", False):
        return _old(self)

    if not self.lrnCount: return False
    if self._lrnQueue: return True
    self._lrnQueue = self.col.db.all("""
select due, id from cards where
did in %s and queue = 1 and due < :lim
limit %d""" % (self._deckLimit(), self.reportLimit), lim=self.dayCutoff)
    if self._lrnQueue:
        r = random.Random()
        r.seed(time.time()*1000)
        r.shuffle(self._lrnQueue)
        self.lrnCount=len(self._lrnQueue)
        return self._lrnQueue
    return False


#####################################################################
## Non-Gui Monkey patch assignment                        ###########
#####################################################################

Reviewer._answerButtonList = wrap(Reviewer._answerButtonList, answerButtonList, 'around')
Reviewer._buttonTime = wrap(Reviewer._buttonTime, buttonTime, 'around')
Scheduler.answerCard = wrap(Scheduler.answerCard, answerCard, 'around')
Scheduler.answerButtons = wrap(Scheduler.answerButtons, answerButtons, 'around')
Scheduler._fillLrn = wrap(Scheduler._fillLrn, fillLrn, 'around')



##################################################
#  Gui stuff
#  Adds deck menu options to enable/disable
#  this addon for specific decks
#################################################
import aqt
import aqt.deckconf
from aqt.qt import *


from anki import version
ANKI21 = version.startswith("2.1.")
if ANKI21:
    from PyQt5 import QtCore, QtGui, QtWidgets
else:
    from PyQt4 import QtCore, QtGui as QtWidgets


try:
    _fromUtf8 = QtCore.QString.fromUtf8
except AttributeError:
    def _fromUtf8(s):
        return s


def dconfsetupUi(self, Dialog):
    if mw.col.sched.name=="std2": return
    r=self.gridLayout_3.rowCount()

    self.sm2emu = QtWidgets.QCheckBox(self.tab_3)
    self.sm2emu.setObjectName(_fromUtf8("sm2emu"))
    self.sm2emu.setText(_('Use the SM2 method for grading'))
    self.gridLayout_3.addWidget(self.sm2emu, r, 0, 1, 3)
    self.sm2emu.toggled.connect(lambda:toggleSM2EmuCB(self))
    r+=1

    self.sm2HLayout = QtWidgets.QHBoxLayout()
    self.sm2HLayout.setObjectName(_fromUtf8("sm2HLayout"))
    self.sm2priorityLabel = QtWidgets.QLabel(Dialog)
    self.sm2priorityLabel.setObjectName(_fromUtf8("sm2priorityLabel"))
    self.sm2priorityLabel.setText(_("SM2++ Priority:"))
    self.sm2HLayout.addWidget(self.sm2priorityLabel)
    self.sm2priority = QtWidgets.QComboBox(self.tab_3)
    self.sm2priority.setObjectName(_fromUtf8("sm2priority"))

    for i,v in PRIORITY_LEVELS.items():
        self.sm2priority.addItem(_fromUtf8(""))
        self.sm2priority.setItemText(i, _(v[0]))

    self.sm2HLayout.addWidget(self.sm2priority)
    self.gridLayout_3.addLayout(self.sm2HLayout, r, 0, 1, 2)


def toggleSM2EmuCB(self):
    off=self.sm2emu.checkState()==0
    on = not off
    self.sm2priority.setDisabled(off)
    self.lrnGradInt.setDisabled(on)
    self.lrnEasyInt.setDisabled(on)
    self.lrnFactor.setDisabled(on)
    self.lapMinInt.setDisabled(on)
    self.lapMult.setDisabled(on)
    self.easyBonus.setDisabled(on)
    # self.fi1.setDisabled(on) #ivl modifier


def loadConf(self):
    if mw.col.sched.name=="std2": return
    idx=self.conf.get("sm2priority", 0)
    self.form.sm2priority.setCurrentIndex(idx)
    cb=self.conf.get("sm2emu", 0)
    self.form.sm2emu.setCheckState(cb)
    toggleSM2EmuCB(self.form)


def saveConf(self):
    if mw.col.sched.name=="std2": return
    self.conf['sm2emu']=self.form.sm2emu.checkState()
    self.conf['sm2priority']=self.form.sm2priority.currentIndex()


aqt.forms.dconf.Ui_Dialog.setupUi = wrap(aqt.forms.dconf.Ui_Dialog.setupUi, dconfsetupUi, pos="after")
aqt.deckconf.DeckConf.loadConf = wrap(aqt.deckconf.DeckConf.loadConf, loadConf, pos="after")
aqt.deckconf.DeckConf.saveConf = wrap(aqt.deckconf.DeckConf.saveConf, saveConf, pos="before")