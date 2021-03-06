"""
Copyright 2016, Blur Studio

This file is part of Simplex.

Simplex is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Simplex is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with Simplex.  If not, see <http://www.gnu.org/licenses/>.

"""

# Ignore a bunch of linter warnings that show up because of my choice of abstraction
#pylint: disable=unused-argument,too-many-public-methods,relative-import
#pylint: disable=too-many-statements,no-self-use,missing-docstring
import os, sys, re, json, copy, weakref
from functools import wraps
from contextlib import contextmanager

# This module imports QT from PyQt4, PySide or PySide2
# Depending on what's available
from SimplexUI.Qt import QtCompat
from SimplexUI.Qt.QtCore import Signal, Slot
from SimplexUI.Qt.QtCore import Qt, QSettings
from SimplexUI.Qt.QtGui import QStandardItemModel
from SimplexUI.Qt.QtWidgets import QMessageBox, QInputDialog, QMenu, QApplication, QTreeView, QDataWidgetMapper
from SimplexUI.Qt.QtWidgets import QMainWindow, QProgressDialog, QPushButton, QComboBox, QCheckBox

from utils import toPyObject, getUiFile, getNextName, makeUnique, naturalSortKey

from interfaceItems import (ProgPair, Slider, Combo, Group, Simplex, Shape, Stack, Falloff)

from interfaceModel import (SliderModel, ComboModel, ComboFilterModel, SliderFilterModel,
							coerceIndexToChildType, coerceIndexToParentType, coerceIndexToRoots,
							SliderGroupModel, FalloffModel, FalloffDataModel, SimplexModel)

from interface import undoContext, rootWindow, DCC, DISPATCH
from plugInterface import loadPlugins, buildToolMenu, buildRightClickMenu
from interfaceModelTrees import SliderTree, ComboTree

from traversalDialog import TraversalDialog
from falloffDialog import FalloffDialog

try:
	# This module is unique to Blur Studio
	import blurdev
except ImportError:
	blurdev = None

NAME_CHECK = re.compile(r'[A-Za-z][\w.]*')

# If the decorated method is a slot for some Qt Signal
# and the method signature is *NOT* the same as the
# signal signature, you must double decorate the method like:
#
# @Slot(**signature)
# @stackable
# def method(**signature)

@contextmanager
def signalsBlocked(item):
	item.blockSignals(True)
	try:
		yield
	finally:
		item.blockSignals(False)


class SimplexDialog(QMainWindow):
	''' The main ui for simplex '''
	simplexLoaded = Signal()
	def __init__(self, parent=None, dispatch=None):
		super(SimplexDialog, self).__init__(parent)

		uiPath = getUiFile(__file__)
		QtCompat.loadUi(uiPath, self)

		# Custom widgets aren't working properly, so I bring them in manually
		self.uiSliderTREE = SliderTree(self.uiMainShapesGRP)
		self.uiSliderTREE.setDragEnabled(False)
		self.uiSliderTREE.setDragDropMode(SliderTree.NoDragDrop)
		self.uiSliderTREE.setSelectionMode(SliderTree.ExtendedSelection)
		self.uiSliderTREE.dragFilter.dragPressed.connect(self.dragStart)
		self.uiSliderTREE.dragFilter.dragReleased.connect(self.dragStop)

		self.uiSliderLAY.addWidget(self.uiSliderTREE)

		self.uiComboTREE = ComboTree(self.uiComboShapesGRP)
		self.uiComboTREE.setDragEnabled(False)
		self.uiComboTREE.setDragDropMode(ComboTree.NoDragDrop)
		self.uiComboTREE.setSelectionMode(ComboTree.ExtendedSelection)
		self.uiComboTREE.dragFilter.dragPressed.connect(self.dragStart)
		self.uiComboTREE.dragFilter.dragReleased.connect(self.dragStop)
		self.uiComboLAY.addWidget(self.uiComboTREE)

		self._sliderMenu = None
		self._comboMenu = None
		self._currentObject = None
		self._currentObjectName = None

		# Connect the combo boxes and spinners to the data model
		# TODO: Figure out how to deal with the type/axis "enum" cboxes
		# see: http://doc.qt.io/qt-5/qtwidgets-itemviews-combowidgetmapper-example.html
		'''
		self._falloffMapper = QDataWidgetMapper()
		self.uiShapeFalloffCBOX.currentIndexChanged.connect(self._falloffMapper.setCurrentIndex)
		'''

		# Make sure to connect the dispatcher to the undo control
		# but only keep a weakref to it
		self.dispatch = None
		if dispatch is not None:
			self.dispatch = weakref.ref(dispatch)
			dispatch.undo.connect(self.handleUndo)
			dispatch.redo.connect(self.handleUndo)
			dispatch.beforeNew.connect(self.newScene)
			dispatch.beforeOpen.connect(self.newScene)

		self.simplex = None

		self._itemMap = {}
		self._sliderTreeMap = {}
		self._comboTreeMap = {}

		self._sliderDrag = None
		self._comboDrag = None

		self.uiSliderExitIsolateBTN.hide()
		self.uiComboExitIsolateBTN.hide()

		self._makeConnections()

		self._toolPlugins, self._contextPlugins = loadPlugins()
		buildToolMenu(self, self._toolPlugins)
		self.uiSliderTREE.setPlugins(self._contextPlugins)
		self.uiComboTREE.setPlugins(self._contextPlugins)

		if DCC.program == "dummy":
			self.getSelectedObject()
			self.setObjectGroupEnabled(False)
			self.setSystemGroupEnabled(False)

		self.uiClearSelectedObjectBTN.hide()
		self.setShapeGroupEnabled(False)
		self.setComboGroupEnabled(False)
		self.setConnectionGroupEnabled(False)
		self.loadSettings()
		self._sliderMul = 2.0 if self.uiDoubleSliderRangeACT.isChecked() else 1.0

		self.travDialog = TraversalDialog(self)
		self.falloffDialog = FalloffDialog(self)
		#self.showTraversalDialog()

	def showTraversalDialog(self):
		self.travDialog.show()
		self.travDialog.setGeometry(30, 30, 400, 400)

	def showFalloffDialog(self):
		self.falloffDialog.show()
		pp = self.falloffDialog.pos()
		x, y = pp.x(), pp.y()
		if x < 0 or y < 0:
			self.falloffDialog.move(max(x, 0), max(y, 0))

	def dragStart(self):
		if self.simplex is not None:
			self.simplex.DCC.undoOpen()

	def dragStop(self):
		if self.simplex is not None:
			self.simplex.DCC.undoClose()

	def storeSettings(self):
		if blurdev is None:
			pref = QSettings("Blur", "Simplex3")
			pref.setValue("geometry", self.saveGeometry())
			pref.sync()
		else:
			pref = blurdev.prefs.find("tools/simplex3")
			pref.recordProperty("geometry", self.saveGeometry())
			pref.save()

	def loadSettings(self):
		if blurdev is None:
			pref = QSettings("Blur", "Simplex3")
			self.restoreGeometry(toPyObject(pref.value("geometry")))
		else:
			pref = blurdev.prefs.find("tools/simplex3")
			geo = pref.restoreProperty('geometry', None)
			if geo is not None:
				self.restoreGeometry(geo)

	def closeEvent(self, event):
		self.storeSettings()
		self.deleteLater()


	# Undo/Redo
	def newScene(self):
		''' Call this before a new scene is created. Usually called from the stack '''
		self.clearSelectedObject()

	def handleUndo(self):
		''' Call this after an undo/redo action. Usually called from the stack '''
		rev = self.simplex.DCC.getRevision()
		data = self.simplex.stack.getRevision(rev)
		if data is not None:
			self.setSystem(data)
			self.uiSliderTREE.setItemExpansion()
			self.uiComboTREE.setItemExpansion()

	def currentSystemChanged(self, idx):
		if idx == -1:
			self.setSystem(None)
			return
		name = str(self.uiCurrentSystemCBOX.currentText())
		if not name:
			self.setSystem(None)
			return
		if self.simplex is not None:
			if self.simplex.name == name:
				return # Do nothing

		pBar = QProgressDialog("Loading from Mesh", "Cancel", 0, 100, self)
		system = Simplex.buildSystemFromMesh(self._currentObject, name, sliderMul=self._sliderMul, pBar=pBar)
		self.setSystem(system)
		pBar.close()

	def setSystem(self, system):
		if system == self.simplex:
			return

		if self.simplex is not None:
			# disconnect the previous stuff
			sliderSelModel = self.uiSliderTREE.selectionModel()
			sliderSelModel.selectionChanged.disconnect(self.unifySliderSelection)
			sliderSelModel.selectionChanged.disconnect(self.populateComboRequirements)

			comboSelModel = self.uiComboTREE.selectionModel()
			comboSelModel.selectionChanged.disconnect(self.unifyComboSelection)

			oldStack = self.simplex.stack
		else:
			oldStack = Stack()

		if system is None:
			#self.toolActions.simplex = None
			self.uiSliderTREE.setModel(QStandardItemModel())
			self.uiComboTREE.setModel(QStandardItemModel())
			self.simplex = system
			self.setShapeGroupEnabled(False)
			self.setComboGroupEnabled(False)
			self.setConnectionGroupEnabled(False)
			self.falloffDialog.loadSimplex()
			self.simplexLoaded.emit()
			return

		# set and connect the new stuff
		self.simplex = system
		self.simplex.models = []
		self.simplex.falloffModels = []
		self.simplex.stack = oldStack

		#self.toolActions.simplex = self.simplex

		simplexModel = SimplexModel(self.simplex, None)

		sliderModel = SliderModel(simplexModel, None)
		sliderProxModel = SliderFilterModel(sliderModel)
		self.uiSliderTREE.setModel(sliderProxModel)
		sliderSelModel = self.uiSliderTREE.selectionModel()
		sliderSelModel.selectionChanged.connect(self.unifySliderSelection)
		sliderSelModel.selectionChanged.connect(self.populateComboRequirements)

		comboModel = ComboModel(simplexModel, None)
		comboProxModel = ComboFilterModel(comboModel)
		self.uiComboTREE.setModel(comboProxModel)
		comboSelModel = self.uiComboTREE.selectionModel()
		comboSelModel.selectionChanged.connect(self.unifyComboSelection)

		self.falloffDialog.loadSimplex()

		# Make sure the UI is up and running
		self.enableComboRequirements()
		self.setShapeGroupEnabled(True)
		self.setComboGroupEnabled(True)
		self.setConnectionGroupEnabled(True)

		self.setSimplexLegacy()
		self.simplexLoaded.emit()

	# UI Setup
	def _makeConnections(self):
		''' Make all the ui connections '''
		# Setup Trees!
		self.uiSliderTREE.setColumnWidth(1, 50)
		self.uiSliderTREE.setColumnWidth(2, 20)
		self.uiSliderFilterLINE.textChanged.connect(self.sliderStringFilter)
		self.uiSliderFilterClearBTN.clicked.connect(self.uiSliderFilterLINE.clear)
		self.uiSliderFilterClearBTN.clicked.connect(self.sliderStringFilter)

		self.uiComboTREE.setColumnWidth(1, 50)
		self.uiComboTREE.setColumnWidth(2, 20)
		self.uiComboFilterLINE.textChanged.connect(self.comboStringFilter)
		self.uiComboFilterClearBTN.clicked.connect(self.uiComboFilterLINE.clear)
		self.uiComboFilterClearBTN.clicked.connect(self.comboStringFilter)

		# dependency setup
		self.uiComboDependAllCHK.stateChanged.connect(self.setAllComboRequirement)
		self.uiComboDependAnyCHK.stateChanged.connect(self.setAnyComboRequirement)
		self.uiComboDependOnlyCHK.stateChanged.connect(self.setOnlyComboRequirement)
		self.uiComboDependLockCHK.stateChanged.connect(self.setLockComboRequirement)

		# Bottom Left Corner Buttons
		self.uiZeroAllBTN.clicked.connect(self.zeroAllSliders)
		self.uiZeroSelectedBTN.clicked.connect(self.zeroSelectedSliders)
		self.uiSelectCtrlBTN.clicked.connect(self.selectCtrl)

		# Top Left Corner Buttons
		self.uiNewGroupBTN.clicked.connect(self.newSliderGroup)
		self.uiNewSliderBTN.clicked.connect(self.newSlider)
		self.uiNewShapeBTN.clicked.connect(self.newSliderShape)
		self.uiSliderDeleteBTN.clicked.connect(self.sliderTreeDelete)

		# Top Right Corner Buttons
		self.uiDeleteComboBTN.clicked.connect(self.comboTreeDelete)
		self.uiNewComboActiveBTN.clicked.connect(self.newActiveCombo)
		self.uiNewComboSelectBTN.clicked.connect(self.newSelectedCombo)
		self.uiNewComboShapeBTN.clicked.connect(self.newComboShape)
		self.uiNewComboGroupBTN.clicked.connect(self.newComboGroup)

		# Bottom right corner buttons
		self.uiSetSliderValsBTN.clicked.connect(self.setSliderVals)
		self.uiSelectSlidersBTN.clicked.connect(self.selectSliders)

		## System level
		self.uiCurrentObjectTXT.editingFinished.connect(self.currentObjectChanged)
		self.uiGetSelectedObjectBTN.clicked.connect(self.getSelectedObject)
		self.uiClearSelectedObjectBTN.clicked.connect(self.clearSelectedObject)

		self.uiNewSystemBTN.clicked.connect(self.newSystem)
		self.uiRenameSystemBTN.clicked.connect(self.renameSystem)
		self.uiCurrentSystemCBOX.currentIndexChanged[int].connect(self.currentSystemChanged)

		# Extraction/connection
		self.uiShapeExtractBTN.clicked.connect(self.shapeExtract)
		self.uiShapeConnectBTN.clicked.connect(self.shapeConnect)
		self.uiShapeConnectSceneBTN.clicked.connect(self.shapeConnectScene)

		# File Menu
		self.uiImportACT.triggered.connect(self.importSystemFromFile)
		self.uiExportACT.triggered.connect(self.exportSystemTemplate)

		# Edit Menu
		self.uiHideRedundantACT.toggled.connect(self.hideRedundant)
		self.uiDoubleSliderRangeACT.toggled.connect(self.setSliderRange)

		# Isolation
		self.uiSliderExitIsolateBTN.clicked.connect(self.sliderTreeExitIsolate)
		self.uiComboExitIsolateBTN.clicked.connect(self.comboTreeExitIsolate)

		#if blurdev is not None:
			#blurdev.core.aboutToClearPaths.connect(self.blurShutdown)

		self.uiLegacyJsonACT.toggled.connect(self.setSimplexLegacy)


	# UI Enable/Disable groups
	def setObjectGroupEnabled(self, value):
		''' Set the Object group enabled value '''
		self._setObjectsEnabled(self.uiObjectGRP, value)

	def setSystemGroupEnabled(self, value):
		''' Set the System group enabled value '''
		self._setObjectsEnabled(self.uiSystemGRP, value)

	def setShapeGroupEnabled(self, value):
		''' Set the Shape group enabled value '''
		self._setObjectsEnabled(self.uiSliderButtonFRM, value)

	def setComboGroupEnabled(self, value):
		''' Set the Combo group enabled value '''
		self._setObjectsEnabled(self.uiComboButtonFRM, value)

	def setConnectionGroupEnabled(self, value):
		''' Set the Connection group enabled value '''
		self._setObjectsEnabled(self.uiConnectionGroupWID, value)

	def _setObjectsEnabled(self, par, value):
		for child in par.children():
			if isinstance(child, (QPushButton, QCheckBox, QComboBox)):
				child.setEnabled(value)


	# Helpers
	def getSelectedItems(self, tree, typ=None):
		''' Convenience function to get the selected system items '''
		sel = tree.selectedIndexes()
		sel = [i for i in sel if i.column() == 0]
		model = tree.model()
		items = [model.itemFromIndex(i) for i in sel]
		if typ is not None:
			items = [i for i in items if isinstance(i, typ)]
		return items

	def getCurrentObject(self):
		return self._currentObject


	# Setup Trees!
	def sliderStringFilter(self):
		''' Set the filter for the slider tree '''
		filterString = str(self.uiSliderFilterLINE.text())
		sliderModel = self.uiSliderTREE.model()
		sliderModel.filterString = str(filterString)
		sliderModel.invalidateFilter()

	def comboStringFilter(self):
		''' Set the filter for the combo tree '''
		filterString = str(self.uiComboFilterLINE.text())
		comboModel = self.uiComboTREE.model()
		comboModel.filterString = str(filterString)
		comboModel.invalidateFilter()


	# selection setup
	def unifySliderSelection(self):
		''' Clear the selection of the combo tree when
		an item on the slider tree is selected '''
		mods = QApplication.keyboardModifiers()
		if not mods & (Qt.ControlModifier | Qt.ShiftModifier):
			comboSelModel = self.uiComboTREE.selectionModel()
			if not comboSelModel:
				return

			with signalsBlocked(comboSelModel):
				comboSelModel.clearSelection()
			self.uiComboTREE.viewport().update()

	def unifyComboSelection(self):
		''' Clear the selection of the slider tree when
		an item on the combo tree is selected '''
		mods = QApplication.keyboardModifiers()
		if not mods & (Qt.ControlModifier | Qt.ShiftModifier):
			sliderSelModel = self.uiSliderTREE.selectionModel()
			if not sliderSelModel:
				return
			with signalsBlocked(sliderSelModel):
				sliderSelModel.clearSelection()
			self.uiSliderTREE.viewport().update()


	# dependency setup
	def setAllComboRequirement(self):
		''' Handle the clicking of the "All" checkbox '''
		self._setComboRequirements(self.uiComboDependAllCHK)

	def setAnyComboRequirement(self):
		''' Handle the clicking of the "Any" checkbox '''
		self._setComboRequirements(self.uiComboDependAnyCHK)

	def setOnlyComboRequirement(self):
		''' Handle the clicking of the "Only" checkbox '''
		self._setComboRequirements(self.uiComboDependOnlyCHK)

	def _setComboRequirements(self, skip):
		items = (self.uiComboDependAnyCHK, self.uiComboDependAllCHK, self.uiComboDependOnlyCHK)
		for item in items:
			if item == skip:
				continue
			if item.isChecked():
				with signalsBlocked(item):
					item.setChecked(False)
		self.enableComboRequirements()

	def setLockComboRequirement(self):
		comboModel = self.uiComboTREE.model()
		if not comboModel:
			return
		self.populateComboRequirements()

	def populateComboRequirements(self):
		''' Let the combo tree know the requirements from the slider tree '''
		items = self.uiSliderTREE.getSelectedItems(Slider)
		comboModel = self.uiComboTREE.model()
		locked = self.uiComboDependLockCHK.isChecked()
		if not locked:
			comboModel.requires = items
		if comboModel.filterRequiresAll or comboModel.filterRequiresAny or comboModel.filterRequiresOnly:
			comboModel.invalidateFilter()

	def enableComboRequirements(self):
		''' Set the requirements for the combo filter model '''
		comboModel = self.uiComboTREE.model()
		if not comboModel:
			return
		comboModel.filterRequiresAll = self.uiComboDependAllCHK.isChecked()
		comboModel.filterRequiresAny = self.uiComboDependAnyCHK.isChecked()
		comboModel.filterRequiresOnly = self.uiComboDependOnlyCHK.isChecked()
		comboModel.invalidateFilter()


	# Bottom Left Corner Buttons
	def zeroAllSliders(self):
		if self.simplex is None:
			return
		sliders = self.simplex.sliders
		weights = [0.0] * len(sliders)
		self.simplex.setSlidersWeights(sliders, weights)
		self.uiSliderTREE.repaint()

	def zeroSelectedSliders(self):
		if self.simplex is None:
			return
		items = self.uiSliderTREE.getSelectedItems(Slider)
		values = [0.0] * len(items)
		self.simplex.setSlidersWeights(items, values)
		self.uiSliderTREE.repaint()

	def selectCtrl(self):
		if self.simplex is None:
			return
		self.simplex.DCC.selectCtrl()


	# Top Left Corner Buttons
	def newSliderGroup(self):
		if self.simplex is None:
			return
		newName, good = QInputDialog.getText(self, "New Group", "Enter a name for the new group", text="Group")
		if not good:
			return
		if not NAME_CHECK.match(newName):
			message = 'Group name can only contain letters and numbers, and cannot start with a number'
			QMessageBox.warning(self, 'Warning', message)
			return
		Group.createGroup(str(newName), self.simplex, groupType=Slider)
		self.uiSliderTREE.model().sourceModel().invalidateFilter()

	def newSlider(self):
		if self.simplex is None:
			return
		# get the new slider name
		newName, good = QInputDialog.getText(self, "New Slider", "Enter a name for the new slider", text="Slider")
		if not good:
			return

		if not NAME_CHECK.match(newName):
			message = 'Slider name can only contain letters and numbers, and cannot start with a number'
			QMessageBox.warning(self, 'Warning', message)
			return

		idxs = self.uiSliderTREE.getSelectedIndexes()
		groups = coerceIndexToParentType(idxs, Group)
		group = groups[0].model().itemFromIndex(groups[0]) if groups else None

		Slider.createSlider(str(newName), self.simplex, group=group)

	def newSliderShape(self):
		pars = self.uiSliderTREE.getSelectedItems(Slider)
		if not pars:
			return
		parItem = pars[0]
		parItem.createShape()
		self.uiSliderTREE.model().invalidateFilter()

	def sliderTreeDelete(self):
		idxs = self.uiSliderTREE.getSelectedIndexes()
		roots = coerceIndexToRoots(idxs)
		if not roots:
			QMessageBox.warning(self, 'Warning', 'Nothing Selected')
			return
		roots = makeUnique([i.model().itemFromIndex(i) for i in roots])
		for r in roots:
			if isinstance(r, Simplex):
				QMessageBox.warning(self, 'Warning', 'Cannot delete a simplex system this way (for now)')
				return

		for r in roots:
			r.delete()
		self.uiSliderTREE.model().invalidateFilter()


	# Top Right Corner Buttons
	def comboTreeDelete(self):
		idxs = self.uiComboTREE.getSelectedIndexes()
		roots = coerceIndexToRoots(idxs)
		roots = makeUnique([i.model().itemFromIndex(i) for i in roots])
		for r in roots:
			r.delete()
		self.uiComboTREE.model().invalidateFilter()

	def newActiveCombo(self):
		if self.simplex is None:
			return
		sliders = []
		values = []
		for s in self.simplex.sliders:
			if s.value != 0.0:
				sliders.append(s)
				values.append(s.value)
		name = Combo.buildComboName(sliders, values)
		newCombo = Combo.createCombo(name, self.simplex, sliders, values)
		self.uiComboTREE.setItemSelection([newCombo])

	def newSelectedCombo(self):
		if self.simplex is None:
			return
		sliders = self.uiSliderTREE.getSelectedItems(Slider)
		values = [1.0] * len(sliders)
		name = Combo.buildComboName(sliders, values)
		newCombo = Combo.createCombo(name, self.simplex, sliders, values)
		self.uiComboTREE.setItemSelection([newCombo])

	def newComboShape(self):
		pars = self.uiComboTREE.getSelectedItems(Combo)
		if not pars:
			return
		parItem = pars[0]
		parItem.createShape()
		pars = self.uiComboTREE.invalidateFilter()

	def newComboGroup(self):
		if self.simplex is None:
			return
		newName, good = QInputDialog.getText(self, "New Group", "Enter a name for the new group", text="Group")
		if not good:
			return
		if not NAME_CHECK.match(newName):
			message = 'Group name can only contain letters and numbers, and cannot start with a number'
			QMessageBox.warning(self, 'Warning', message)
			return
		Group.createGroup(str(newName), self.simplex, groupType=Combo)
		self.uiComboTREE.model().sourceModel().invalidateFilter()


	# Bottom right corner buttons
	def setSliderVals(self):
		if self.simplex is None:
			return
		combos = self.uiComboTREE.getSelectedItems(Combo)
		# maybe coerce to type instead??
		self.zeroAllSliders()
		values = []
		sliders = []
		for combo in combos:
			for pair in combo.pairs:
				if pair.slider in sliders:
					continue
				sliders.append(pair.slider)
				values.append(pair.value)
		self.simplex.setSlidersWeights(sliders, values)
		self.uiSliderTREE.repaint()

	def selectSliders(self):
		combos = self.uiComboTREE.getSelectedItems(Combo)
		sliders = []
		for combo in combos:
			for pair in combo.pairs:
				sliders.append(pair.slider)
		self.uiSliderTREE.setItemSelection(sliders)


	# Extraction/connection
	def shapeConnectScene(self):
		if self.simplex is None:
			return
		# make a dict of name:object
		sel = DCC.getSelectedObjects()
		selDict = {}
		for s in sel:
			name = DCC.getObjectName(s)
			if name.endswith("_Extract"):
				nn = name.rsplit("_Extract", 1)[0]
				selDict[nn] = s

		pairDict = {}
		for p in self.simplex.progs:
			for pp in p.pairs:
				pairDict[pp.shape.name] = pp

		# get all common names
		selKeys = set(selDict.iterkeys())
		pairKeys = set(pairDict.iterkeys())
		common = selKeys & pairKeys

		# get those items
		pairs = [pairDict[i] for i in common]

		# Set up the progress bar
		pBar = QProgressDialog("Connecting Shapes", "Cancel", 0, 100, self)
		pBar.setMaximum(len(pairs))

		# Do the extractions
		for pair in pairs:
			c = pair.prog.controller
			c.connectShape(pair.shape, delete=True)

			# ProgressBar
			pBar.setValue(pBar.value() + 1)
			pBar.setLabelText("Connecting:\n{0}".format(pair.shape.name))
			QApplication.processEvents()
			if pBar.wasCanceled():
				return

		pBar.close()


	def sliderShapeExtract(self):
		# Create meshes that are possibly live-connected to the shapes
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		self.shapeIndexExtract(sliderIdxs)

	def comboShapeExtract(self):
		# Create meshes that are possibly live-connected to the shapes
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeIndexExtract(comboIdxs)

	def shapeExtract(self):
		# Create meshes that are possibly live-connected to the shapes
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeIndexExtract(sliderIdxs + comboIdxs)

	def shapeIndexExtract(self, indexes, live=None):
		# Create meshes that are possibly live-connected to the shapes
		if live is None:
			live = self.uiLiveShapeConnectionACT.isChecked()

		pairs = coerceIndexToChildType(indexes, ProgPair)
		pairs = [i.model().itemFromIndex(i) for i in pairs]
		pairs = makeUnique([i for i in pairs if not i.shape.isRest])
		pairs.sort(key=lambda x: naturalSortKey(x.shape.name))

		# Set up the progress bar
		pBar = QProgressDialog("Extracting Shapes", "Cancel", 0, 100, self)
		pBar.setMaximum(len(pairs))

		# Do the extractions
		offset = 10
		for pair in pairs:
			c = pair.prog.controller
			c.extractShape(pair.shape, live=live, offset=offset)
			offset += 5

			# ProgressBar
			pBar.setValue(pBar.value() + 1)
			pBar.setLabelText("Extracting:\n{0}".format(pair.shape.name))
			QApplication.processEvents()
			if pBar.wasCanceled():
				return

		pBar.close()


	def sliderShapeConnect(self):
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		self.shapeConnectIndexes(sliderIdxs)

	def comboShapeConnect(self):
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeConnectIndexes(comboIdxs)

	def shapeConnect(self):
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeConnectIndexes(sliderIdxs + comboIdxs)

	def shapeConnectIndexes(self, indexes):
		pairs = coerceIndexToChildType(indexes, ProgPair)
		pairs = [i.model().itemFromIndex(i) for i in pairs]
		pairs = makeUnique([i for i in pairs if not i.shape.isRest])

		# Set up the progress bar
		pBar = QProgressDialog("Connecting Shapes", "Cancel", 0, 100, self)
		pBar.setMaximum(len(pairs))

		# Do the extractions
		for pair in pairs:
			c = pair.prog.controller
			c.connectShape(pair.shape, delete=True)

			# ProgressBar
			pBar.setValue(pBar.value() + 1)
			pBar.setLabelText("Extracting:\n{0}".format(pair.shape.name))
			QApplication.processEvents()
			if pBar.wasCanceled():
				return

		pBar.close()


	def sliderShapeMatch(self):
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		self.shapeMatchIndexes(sliderIdxs)

	def comboShapeMatch(self):
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeMatchIndexes(comboIdxs)

	def shapeMatch(self):
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeMatchIndexes(sliderIdxs + comboIdxs)

	def shapeMatchIndexes(self, indexes):
		# make a dict of name:object
		sel = DCC.getSelectedObjects()
		if not sel:
			return
		mesh = sel[0]

		pairs = coerceIndexToChildType(indexes, ProgPair)
		pairs = [i.model().itemFromIndex(i) for i in pairs]
		pairs = makeUnique([i for i in pairs if not i.shape.isRest])

		# Set up the progress bar
		pBar = QProgressDialog("Matching Shapes", "Cancel", 0, 100, self)
		pBar.setMaximum(len(pairs))

		# Do the extractions
		for pair in pairs:
			c = pair.prog.controller
			c.connectShape(pair.shape, mesh=mesh)

			# ProgressBar
			pBar.setValue(pBar.value() + 1)
			pBar.setLabelText("Matching:\n{0}".format(pair.shape.name))
			QApplication.processEvents()
			if pBar.wasCanceled():
				return

		pBar.close()


	def sliderShapeClear(self):
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		self.shapeClearIndexes(sliderIdxs)

	def comboShapeClear(self):
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeClearIndexes(comboIdxs)

	def shapeClear(self):
		sliderIdxs = self.uiSliderTREE.getSelectedIndexes()
		comboIdxs = self.uiComboTREE.getSelectedIndexes()
		self.shapeClearIndexes(sliderIdxs + comboIdxs)

	def shapeClearIndexes(self, indexes):
		pairs = coerceIndexToChildType(indexes, ProgPair)
		pairs = [i.model().itemFromIndex(i) for i in pairs]
		pairs = makeUnique([i for i in pairs if not i.shape.isRest])

		for pair in pairs:
			pair.shape.zeroShape()


	# System level
	def loadObject(self, thing):
		if thing is None:
			return

		self.uiClearSelectedObjectBTN.show()
		self.uiCurrentSystemCBOX.clear()
		objName = DCC.getObjectName(thing)
		self._currentObject = thing
		self._currentObjectName = objName
		self.uiCurrentObjectTXT.setText(objName)

		ops = DCC.getSimplexOperatorsOnObject(self._currentObject)

		for op in ops:
			js = DCC.getSimplexString(op)
			if not js:
				continue
			d = json.loads(js)
			name = d["systemName"]
			self.uiCurrentSystemCBOX.addItem(name, (self._currentObject, name))

	def currentObjectChanged(self):
		name = str(self.uiCurrentObjectTXT.text())
		if self._currentObjectName == name:
			return
		if not name:
			return

		newObject = DCC.getObjectByName(name)
		if not newObject:
			return

		self.loadObject(newObject)

	def getSelectedObject(self):
		sel = DCC.getSelectedObjects()
		if not sel:
			return
		newObj = sel[0]
		if not newObj:
			return
		self.loadObject(newObj)

	def clearSelectedObject(self):
		self.uiClearSelectedObjectBTN.hide()
		self.uiCurrentSystemCBOX.clear()
		self._currentObject = None
		self._currentObjectName = None
		self.uiCurrentObjectTXT.setText('')
		self.setSystem(None)
		# Clear the current system

	def newSystem(self):
		if self._currentObject is None:
			QMessageBox.warning(self, 'Warning', 'Must have a current object selection')
			return

		newName, good = QInputDialog.getText(self, "New System", "Enter a name for the new system")
		if not good:
			return

		newName = str(newName)
		if not NAME_CHECK.match(newName):
			message = 'System name can only contain letters and numbers, and cannot start with a number'
			QMessageBox.warning(self, 'Warning', message)
			return

		newSystem = Simplex.buildEmptySystem(self._currentObject, newName, sliderMul=self._sliderMul)
		with signalsBlocked(self.uiCurrentSystemCBOX):
			self.uiCurrentSystemCBOX.addItem(newName)
			self.uiCurrentSystemCBOX.setCurrentIndex(self.uiCurrentSystemCBOX.count()-1)
			self.setSystem(newSystem)

	def renameSystem(self):
		if self.simplex is None:
			return
		nn, good = QInputDialog.getText(self, "New System Name", "Enter a name for the System", text=self.simplex.name)
		if not good:
			return

		if not NAME_CHECK.match(nn):
			message = 'System name can only contain letters and numbers, and cannot start with a number'
			QMessageBox.warning(self, 'Warning', message)
			return

		sysNames = [str(self.uiCurrentSystemCBOX.itemText(i)) for i in range(self.uiCurrentSystemCBOX.count())]
		nn = getNextName(nn, sysNames)
		self.simplex.name = nn

		idx = self.uiCurrentSystemCBOX.currentIndex()
		self.uiCurrentSystemCBOX.setItemText(idx, nn)

		self.currentSystemChanged(idx)

	def setSimplexLegacy(self):
		if self.simplex is not None:
			self.simplex.setLegacy(self.uiLegacyJsonACT.isChecked())
			self.simplex.DCC.incrementRevision()


	# File Menu
	def importSystemFromFile(self):
		if self._currentObject is None:
			impTypes = ['smpx']
		else:
			impTypes = ['smpx', 'json']

		if blurdev is None:
			pref = QSettings("Blur", "Simplex3")
			defaultPath = str(toPyObject(pref.value('systemImport', os.path.join(os.path.expanduser('~')))))
			path = self.fileDialog("Import Template", defaultPath, impTypes, save=False)
			if not path:
				return
			pref.setValue('systemImport', os.path.dirname(path))
			pref.sync()
		else:
			# Blur Prefs
			pref = blurdev.prefs.find('tools/simplex3')
			defaultPath = pref.restoreProperty('systemImport', os.path.join(os.path.expanduser('~')))
			path = self.fileDialog("Import Template", defaultPath, impTypes, save=False)
			if not path:
				return
			pref.recordProperty('systemImport', os.path.dirname(path))
			pref.save()

		pBar = QProgressDialog("Loading Shapes", "Cancel", 0, 100, self)
		pBar.show()
		QApplication.processEvents()

		# TODO: Come up with a better list of possibilites for loading
		# simplex files, and make the appropriate methods on the Simplex
		if path.endswith('.smpx'):
			newSystem = Simplex.buildSystemFromSmpx(path, self._currentObject, sliderMul=self._sliderMul, pBar=pBar)
		elif path.endswith('.json'):
			newSystem = Simplex.buildSystemFromJson(path, self._currentObject, sliderMul=self._sliderMul, pBar=pBar)
		
		with signalsBlocked(self.uiCurrentSystemCBOX):
			self.loadObject(newSystem.DCC.mesh)
			idx = self.uiCurrentSystemCBOX.findText(self._currentObjectName)
			if idx >= 0:
				self.uiCurrentSystemCBOX.setCurrentIndex(idx)
			else:
				self.uiCurrentSystemCBOX.addItem(newSystem.name)
				self.uiCurrentSystemCBOX.setCurrentIndex(self.uiCurrentSystemCBOX.count()-1)

		self.setSystem(newSystem)
		pBar.close()

	def fileDialog(self, title, initPath, filters, save=True):
		filters = ["{0} (*.{0})".format(f) for f in filters]
		if not save:
			filters += ["All files (*.*)"]
		filters = ";;".join(filters)

		if save:
			path, _ = QtCompat.QFileDialog.getSaveFileName(self, title, initPath, filters)
		else:
			path, _ = QtCompat.QFileDialog.getOpenFileName(self, title, initPath, filters)

		if not path:
			return ''

		if not save and not os.path.exists(path):
			return ''

		return path

	def exportSystemTemplate(self):
		if self._currentObject is None:
			QMessageBox.warning(self, 'Warning', 'Must have a current object selection')
			return

		if blurdev is None:
			pref = QSettings("Blur", "Simplex3")
			defaultPath = str(toPyObject(pref.value('systemExport', os.path.join(os.path.expanduser('~')))))
			path = self.fileDialog("Export Template", defaultPath, ["smpx", "json"], save=True)
			if not path:
				return
			pref.setValue('systemExport', os.path.dirname(path))
			pref.sync()
		else:
			# Blur Prefs
			pref = blurdev.prefs.find('tools/simplex3')
			defaultPath = pref.restoreProperty('systemExport', os.path.join(os.path.expanduser('~')))
			path = self.fileDialog("Export Template", defaultPath, ["smpx", "json"], save=True)
			if not path:
				return
			pref.recordProperty('systemExport', os.path.dirname(path))
			pref.save()

		if path.endswith('.smpx'):
			pBar = QProgressDialog("Exporting smpx File", "Cancel", 0, 100, self)
			pBar.show()
			self.simplex.exportAbc(path, pBar)
			pBar.close()
		elif path.endswith('.json'):
			dump = self.simplex.dump()
			with open(path, 'w') as f:
				f.write(dump)

	def importObjFolder(self, folder):
		''' Import all objs from a user selected folder '''
		if not os.path.isdir(folder):
			QMessageBox.warning(self, 'Warning', 'Folder does not exist')
			return

		paths = os.listdir(folder)
		paths = [i for i in paths if i.endswith('.obj')]
		if not paths:
			QMessageBox.warning(self, 'Warning', 'Folder does not contain any .obj files')
			return

		shapeDict = {shape.name: shape for shape in self.simplex.shapes}

		inPairs = {}
		for path in paths:
			shapeName = os.path.splitext(os.path.basename(path))[0]
			shape = shapeDict.get(shapeName)
			if shape is not None:
				inPairs[shapeName] = path
			else:
				sfx = "_Extract"
				if shapeName.endswith(sfx):
					shapeName = shapeName[:-len(sfx)]
					shape = shapeDict.get(shapeName)
					if shape is not None:
						inPairs[shapeName] = path

		sliderMasters, comboMasters = {}, {}
		for masters in [self.simplex.sliders, self.simplex.combos]:
			for master in masters:
				for pp in master.prog.pairs:
					shape = shapeDict.get(pp.shape.name)
					if shape is not None:
						if shape.name in inPairs:
							if isinstance(master, Slider):
								sliderMasters[shape.name] = master
							if isinstance(master, Combo):
								comboMasters[shape.name] = master

		comboDepth = {}
		for k, v in comboMasters.iteritems():
			depth = len(v.pairs)
			comboDepth.setdefault(depth, {})[k] = v

		pBar = QProgressDialog("Loading from Mesh", "Cancel", 0, len(comboMasters) + len(sliderMasters), self)
		pBar.show()

		for shapeName, slider in sliderMasters.iteritems():
			pBar.setValue(pBar.value() + 1)
			pBar.setLabelText("Loading Obj :\n{0}".format(shapeName))
			QApplication.processEvents()
			if pBar.wasCanceled():
				return

			path = inPairs[shapeName]
			mesh = self.simplex.DCC.importObj(os.path.join(folder, path))

			shape = shapeDict[shapeName]
			self.simplex.DCC.connectShape(shape, mesh=mesh, live=False, delete=True)

		for depth in sorted(comboDepth.keys()):
			for shapeName, combo in comboDepth[depth].iteritems():
				pBar.setValue(pBar.value() + 1)
				pBar.setLabelText("Loading Obj :\n{0}".format(shapeName))
				QApplication.processEvents()
				if pBar.wasCanceled():
					return

				path = inPairs[shapeName]
				mesh = self.simplex.DCC.importObj(os.path.join(folder, path))
				shape = shapeDict[shapeName]
				self.simplex.DCC.connectComboShape(combo, shape, mesh=mesh, live=False, delete=True)

		pBar.close()


	# Slider Settings
	def setSelectedSliderGroups(self, group):
		if not group:
			return
		sliders = self.uiSliderTREE.getSelectedItems(Slider)
		group.take(sliders)
		self.uiSliderTREE.viewport().update()

	def setSelectedSliderFalloff(self, falloff, state):
		if not falloff:
			return
		sliders = self.uiSliderTREE.getSelectedItems(Slider)
		for s in sliders:
			if state == Qt.Checked:
				s.prog.addFalloff(falloff)
			else:
				s.prog.removeFalloff(falloff)
		self.uiSliderTREE.viewport().update()

	def setSelectedSliderInterp(self, interp):
		sliders = self.uiSliderTREE.getSelectedItems(Slider)
		for s in sliders:
			s.prog.interp = interp

	def loadShapeName(self):
		progPairs = self.uiSliderTREE.getSelectedItems(ProgPair)
		with signalsBlocked(self.uiShapeNameTXT):
			names = set([pp.shape.name for pp in progPairs])
			if len(names) == 1:
				name = names.pop()
				self.uiShapeNameTXT.setEnabled(True)
				self.uiShapeNameTXT.setText(name)
			elif len(names) == 0:
				self.uiShapeNameTXT.setEnabled(False)
				self.uiShapeNameTXT.setText("None ...")
			else:
				self.uiShapeNameTXT.setEnabled(False)
				self.uiShapeNameTXT.setText("Multi ...")

	def setShapeName(self):
		progPairs = self.uiSliderTREE.getSelectedItems(ProgPair)
		if len(progPairs) != 1:
			message = 'You can set exactly one shape name at a time this way'
			QMessageBox.warning(self, 'Warning', message)
			return

		newName = self.uiShapeNameTXT.text()
		if not NAME_CHECK.match(newName):
			message = 'Slider name can only contain letters and numbers, and cannot start with a number'
			QMessageBox.warning(self, 'Warning', message)
			return
		progPairs[0].shape.name = newName
		self.uiSliderTREE.viewport().update()


	# Combo Settings
	def setSelectedComboSolveType(self, stVal):
		combos = self.uiComboTREE.getSelectedItems(Combo)
		for c in combos:
			c.solveType = stVal


	# Edit Menu
	def hideRedundant(self):
		check = self.uiHideRedundantACT.isChecked()
		comboModel = self.uiComboTREE.model()
		comboModel.filterShapes = check
		comboModel.invalidateFilter()
		sliderModel = self.uiSliderTREE.model()
		sliderModel.doFilter = check
		sliderModel.invalidateFilter()

	def setSliderRange(self):
		self._sliderMul = 2.0 if self.uiDoubleSliderRangeACT.isChecked() else 1.0
		if self.simplex is None:
			return
		self.simplex.DCC.sliderMul = self._sliderMul
		self.simplex.DCC.setSlidersRange(self.simplex.sliders)


	# Isolation
	def isSliderIsolate(self):
		model = self.uiSliderTREE.model()
		if model:
			return bool(model.isolateList)
		return False

	def sliderIsolateSelected(self):
		self.uiSliderTREE.isolateSelected()
		self.uiSliderExitIsolateBTN.show()

	def sliderTreeExitIsolate(self):
		self.uiSliderTREE.exitIsolate()
		self.uiSliderExitIsolateBTN.hide()

	def isComboIsolate(self):
		model = self.uiComboTREE.model()
		if model:
			return bool(model.isolateList)
		return False

	def comboIsolateSelected(self):
		self.uiComboTREE.isolateSelected()
		self.uiComboExitIsolateBTN.show()

	def comboTreeExitIsolate(self):
		self.uiComboTREE.exitIsolate()
		self.uiComboExitIsolateBTN.hide()


def _test():
	app = QApplication(sys.argv)
	path = r'C:\Users\tfox\Documents\GitHub\Simplex\scripts\SimplexUI\build\HeadMaleStandard_High_Unsplit.smpx'
	d = SimplexDialog()
	newSystem = Simplex.buildSystemFromSmpx(path, d.getCurrentObject(), sliderMul=1.0)
	d.setSystem(newSystem)

	d.show()
	sys.exit(app.exec_())

if __name__ == '__main__':
	_test()


