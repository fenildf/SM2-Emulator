# -*- coding: utf-8 -*-
# Copyright: (C) 2018 Lovac42
# Support: https://github.com/lovac42/SM2-Emulator
# License: GNU GPL, version 3 or later; http://www.gnu.org/copyleft/gpl.html
# Version: 0.1.2


from __future__ import division
# == User Config =========================================

#No added bonuses, but still allows for different profiles
DEFAULT_SM2_BEHAVIOR = False

#FACTOR ADD/SUB
INC_FACTOR = 100   #EasyBtn: 100 sm2, 150 anki
DEC_FACTOR = -140  #HardBtn: -140 sm2, -150 anki
ALT_FACTOR = 0     #AgainBtn: 0 sm2, -200 anki, -160 Mnemosyne

# == End Config ==========================================
##########################################################


from aqt import mw
from anki.hooks import wrap, addHook
from aqt.reviewer import Reviewer
from anki.sched import Scheduler
from anki.utils import intTime, fmtTimeSpan, ids2str
# from aqt.utils import showWarning, showText
from heapq import *
import time, random


#Initial Intervals
DYNAMIC_IVL=False
INIT_IVL=1
SEC_IVL =6    #anki: 1*EF or about 2-3 days after fuzz
BUMP_IVL=21   #Breaks out of low interval hell

# idx:[ display_name, dynamic, initial, secondary]
PRIORITY_LEVELS = {
  0:["Normal (SM2)",    False, 1,  6], #sm2 default
  1:["Slacker",         False, 3,  7],
  2:["Vacation",        False, 5, 14],
  3:["Beefcake (Anki)", False, 1,  3], #similar to anki's default config
  4:["Defer Leech",     True,  4, 10], #1:6 for new, 4:10 max for leech cards (+LB)
}


#####################################################################
####   Filters, don't apply addon to certain models  ################
#####################################################################
isFilteredCard = False
isRevertedCard = False

def isFiltered():
    if mw.col.sched.name=="std2":
        return True

    card = mw.reviewer.card
    conf = mw.col.decks.confForDid(card.did)
    if conf['dyn']:
        if not conf['resched']: return True
        conf = mw.col.decks.confForDid(card.odid)

    if not conf.get("sm2emu", False):
        return True

    model = card.model()['name']
    if model=='IR3' or model[:6]=='IRead2': #Avoid IR Cards
        return True

    return False


def onShowQuestion():
    global isFilteredCard, isRevertedCard
    isFilteredCard=isFiltered()
    if not isFilteredCard:
        c=mw.reviewer.card
        conf=mw.col.decks.confForDid(c.odid or c.did)
        adjustPriorityInterval(c, conf)
        isRevertedCard=isReverted(c)
addHook('showQuestion', onShowQuestion)


def adjustPriorityInterval(card, conf):
    global INIT_IVL, SEC_IVL, DYNAMIC_IVL
    level=conf.get("sm2priority", 0)
    assert level < len(PRIORITY_LEVELS)
    DYNAMIC_IVL=PRIORITY_LEVELS[level][1]
    INIT_IVL=PRIORITY_LEVELS[level][2]
    SEC_IVL=PRIORITY_LEVELS[level][3]
    return DYNAMIC_IVL #bool


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
    if isFilteredCard:
        return _old(self, i)

    c=self.card
    text=None
    if i==1:
        if c.ivl<21: #Shows profile name
            conf=mw.col.decks.confForDid(c.odid or c.did)
            level=conf.get("sm2priority", 0)
            text=PRIORITY_LEVELS[level][0]
        elif DEFAULT_SM2_BEHAVIOR:
            text="IVL 0"
        else:
            text='Revert'
        return '<font color="pink" class="nobold">%s</font><br>'%text

    elif i==2:
        text='%.1f EF'%(adjustFactor(c,DEC_FACTOR)/1000.0)
        return '<font color="gray" class="nobold">%s</font><br>'%text

    elif i==3:
        extra='1d, ' if isRevertedCard else ''
        text=nextIntervalString(c, i)
        return '<font color="aqua" class="nobold">%s%s</font><br>'%(extra,text)

    elif i==4:
        if c.queue not in (1,3) and c.ivl<=INIT_IVL:
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


LOG_LEARNED=0
LOG_REVIEWED=1
LOG_RELEARNED=2
LOG_CRAM=3
LOG_RESCHED=4

def answerCard(self, card, ease, _old):
    if isFilteredCard:
        return _old(self, card, ease)

    self.col.log()
    assert ease >= 1 and ease <= 4
    self.col.markReview(card) #for undo
    if self._burySiblingsOnAnswer:
        self._burySiblings(card)

    card.factor = adjustFactor(card,0) #initialize new/malformed cards

    #LOG TIME (for Display only)
    delay=0
    if card.queue==2:
        card.lastIvl = card.ivl
    elif card.queue==3:
        card.lastIvl = -86400 #1d learning step in secs
    else:
        card.lastIvl = -getDelay(self, card)

    #LOG TYPE
    revType = 'rev'
    logType = LOG_REVIEWED
    if card.type==0 and card.queue==0:
        logType = LOG_LEARNED
        revType = 'new'
    elif card.odid:
        if card.queue!=2:
            logType=LOG_CRAM
            revType = 'lrn'
    elif card.queue in (1,3):
        logType=LOG_RELEARNED if card.type==2 else LOG_LEARNED
        revType = 'lrn'


    #PROCESS GRADES
    if ease==1: #reset young, revert matured
        card.ivl=revertInterval(card)
        if not isLeechCard(card): #chk suspend
            delay=repeatCard(self, card) #sets queue to 1

    elif ease==2: #repeat, -140ef
        if not DEFAULT_SM2_BEHAVIOR and card.factor==1300:
            card.ivl=max(INIT_IVL, int(card.ivl*0.95))
        card.factor=adjustFactor(card, DEC_FACTOR)
        delay=repeatCard(self, card) #sets queue to 1

    elif ease<=4: #advance
        #Repeats an extra day to avoid judgement of learning bias (not in SM2)
        if isRevertedCard:
            delay=repeatCard(self, card, 1) #sets queue to 3
        else:
            idealIvl = nextInterval(self, card, ease)
            card.ivl = custFuzzedIvl(self.today, idealIvl, card.queue)
            card.due = self.today + card.ivl
            card.type = card.queue = 2
            card.left = 0

        if ease==4: #Mnemosyne adds this value first, anki adds this last, makes little diff to IVL
            card.factor=adjustFactor(card, INC_FACTOR)
        if card.odid:
            card.did = card.odid
            card.odid=card.odue=0

    #LOG THIS REVIEW
    logStats(card, ease, logType, delay)
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


def getEaseFactor(card, ease=3, overdue=0):
    fct=adjustFactor(card, -overdue)
    if DEFAULT_SM2_BEHAVIOR or card.reps<5: #Not enough data
        return fct/1000.0

    #Trim EF based on number of lapses
    lr=card.lapses/card.reps #Leech Ratio
    if ease==4 and card.queue not in (1,3):
        if card.ivl>21:
            fct=max(1.2, fct * (1.05-lr) / 1000.0)
        else:
            fct=max(1.3, fct * (1.15-lr) / 1000.0)
    else: #ease3
        fct=max(1.2, fct * (1-lr) / 1000.0)
    return min(3, fct) #TODO: find max optimal value.


def nextIntervalString(card, ease): #button date display
    ivl=nextInterval(mw.col.sched, card, ease)
    return fmtTimeSpan(ivl*86400, short=True)


def nextInterval(self, card, ease):
    if ease==4 and card.queue not in (1,3) and card.ivl<=INIT_IVL:
        return random.randint(BUMP_IVL-1, BUMP_IVL+2)
    if card.queue==3: #Day learning cards
        return card.ivl+1

    conf=mw.col.decks.confForDid(card.odid or card.did)

    #In cases where user switches profiles,
    #creating a large gap between init ivls.
    idealIvl=INIT_IVL
    if DYNAMIC_IVL:
        ef=getEaseFactor(card, ease)
        idealIvl -= (ef-1.3)*3/1.2

    #Can't use 0 based ivl in anki as that is considered an unfixed error.
    if card.ivl<idealIvl or (card.queue==1 and card.ivl<=idealIvl):
        if card.queue==2:
            idealIvl+=card.ivl+1

    elif card.ivl<SEC_IVL: #large ivl gap when user switch profiles.
        idealIvl=SEC_IVL
        if DYNAMIC_IVL: #possible looping w/ multi profiles
            ef=getEaseFactor(card, ease)
            idealIvl -= (ef-1.3)*INIT_IVL/1.2
            idealIvl = max(card.ivl+INIT_IVL,idealIvl) #loop breaker

    else:
        overdue = 0
        if card.queue==2 and card.ivl>=21:
            # Note: due on learning cards count by secs
            #       due on review cards count by days
            #slight punishment for reviewing ahead.
            overdue = max(-10, self.today - (card.odue or card.due))
            overdue = min(card.ivl, min(100, overdue)) #paused young decks
        ef=getEaseFactor(card, ease, overdue)
        #IVL*modifier may result in smaller IVL
        modifier=conf['rev'].get('ivlFct', 1)
        idealIvl = (card.ivl + overdue // 2) * ef * modifier
        #prevent smaller ivls from %modifier%
        idealIvl = max(card.ivl+1, idealIvl)

    return min(int(idealIvl), conf['rev']['maxIvl'])


#REPLACE RANDOMIZED DATES WITH LOAD BALANCING.
#Some codes came from anki.sched.Scheduler.dueForecast.
def custFuzzedIvl(today, ivl, queue=2):
    if DEFAULT_SM2_BEHAVIOR: return ivl

    if ivl<=1 or (not DYNAMIC_IVL and queue==1):
        return ivl #exact date for hard/agained

    minDay, maxDay = custFuzzIvlRange(ivl)
    if DYNAMIC_IVL and ivl<=SEC_IVL:
        maxDay+=ivl//5 #LB: 0 under 5, +1 under 10d, +2 on 10d

    if minDay<90 and random.randint(0,6): #introduce noise, 15% noise
        #In cases of paused decks, balancing per deck is preferred.
        #But not in cases where there are too many sub-decks.
        perDeck=""
        if maxDay>32 and random.randint(0,4): #2d overlap, 20% noise
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


def custFuzzIvlRange(ivl): # Multiples of 7
    if ivl <  7: return [ivl,   ivl+1]  #r2, x1
    if ivl < 21: return [ivl-1, ivl+1]  #r3, x3
    if ivl < 42: return [ivl-1, ivl+2]  #r4, x6
    if ivl < 84: return [ivl-2, ivl+2]  #r5, x12
    if ivl <168: return [ivl-2, ivl+3]  #r6, x24
    return [ivl-3, ivl+3] #max range 7


#####################################################################
#######          Utils                                ##############
#####################################################################


#log type
#0 = learned
#1 = review
#2 = relearned
#3 = filtered, not used here

def logStats(card, ease, type, delay): #copied & modded from anki.sched.logStats
    def log():
        mw.col.db.execute(
            "insert into revlog values (?,?,?,?,?,?,?,?,?)",
            int(time.time()*1000), card.id, mw.col.usn(), ease,
            -delay or card.ivl or 1, card.lastIvl,
            card.factor, card.timeTaken(), type)
    try:
        log()
    except:
        time.sleep(0.01) # duplicate pk; retry in 10ms
        log()


def isLeechCard(card):
    if card.queue<2: return False
    #rev and day lrn cards only
    card.lapses += 1
    conf=mw.col.sched._lapseConf(card)
    leech=mw.col.sched._checkLeech(card,conf)
    return leech and card.queue == -1


#Inspired by the addon "Another Retreat"
def revertInterval(card):
    if DEFAULT_SM2_BEHAVIOR: return 1
    if card.ivl < 21 or card.queue==3: return 1
    lim=card.ivl//1.2 #In case of bad mods causing large gaps in revlog.
    hist = mw.col.db.list("""
select ivl from revlog where cid = ? 
and type < 3 and ivl between 21 and ?
order by id desc limit 100""", card.id, lim)
    if hist:
        ret=hist[0]
    else:
        ret=card.ivl//2.5
    card.factor=adjustFactor(card, ALT_FACTOR)
    return 1 if ret<21 else ret


def repeatCard(self, card, days=0):
    #Note: new cards in learning steps: card.type=1
    #      lapse cards in learning steps: card.type=2
    card.type=2 if card.type==2 else 1
    card.left = 1001
    if days:
        delay = 86400 #1d learning step in secs
        card.due = self.today + days
        card.queue = 3
    else:
        delay=getDelay(self, card)
        card.due = intTime() + delay
        card.queue = 1
        self.lrnCount += 1
        heappush(self._lrnQueue, (card.due, card.id))
    return delay


def getDelay(self, card):
    conf=self._lrnConf(card)
    return self._delayForGrade(conf,0)


#A process intense way to separate out ease1 or ease2 repeats
#while still being compliant with Anki
def isReverted(card):
    if DEFAULT_SM2_BEHAVIOR: return False
    if card.queue != 1: return False
    if card.ivl   < 21: return False
    en=mw.col.db.all("""
select type, ease from revlog
where type < 3 and cid = ?
order by id desc limit 20""", card.id)
    if en: #filter out ease1 from type 1 or 2
        for (t, e) in en:
            if e==1 and t in (LOG_REVIEWED,LOG_RELEARNED):
                return True
            if t==LOG_REVIEWED: break #limit breaker
    return False


#####################################################################
## Non-Gui Monkey patch assignment                        ###########
#####################################################################

Reviewer._answerButtonList = wrap(Reviewer._answerButtonList, answerButtonList, 'around')
Reviewer._buttonTime = wrap(Reviewer._buttonTime, buttonTime, 'around')
Scheduler.answerCard = wrap(Scheduler.answerCard, answerCard, 'around')
Scheduler.answerButtons = wrap(Scheduler.answerButtons, answerButtons, 'around')


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

    try: #no plan0 addon
        if on and self.sm0emu.checkState():
            self.sm0emu.setCheckState(0)
            self.sm0Steps.setDisabled(True)
    except: pass

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
