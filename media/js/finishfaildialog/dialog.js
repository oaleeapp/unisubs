// Amara, universalsubtitles.org
//
// Copyright (C) 2012 Participatory Culture Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see
// http://www.gnu.org/licenses/agpl-3.0.html.

goog.provide('unisubs.finishfaildialog.Dialog');

/**
 * @constructor
 * @param {unisubs.subtitle.EditableCaptionSet} captionSet
 * @param {?number} status
 * @param {function()} saveFn
 */
unisubs.finishfaildialog.Dialog = function(captionSet, status, saveFn) {
    goog.ui.Dialog.call(this, 'unisubs-modal-lang', true);
    this.setButtonSet(null);
    this.setDisposeOnHide(true);
    this.captionSet_ = captionSet;
    this.status_ = status;
    this.saveFn_ = saveFn;
};
goog.inherits(unisubs.finishfaildialog.Dialog, goog.ui.Dialog);

/**
 * Called to show the finish fail dialog.
 * @param {unisubs.subtitle.EditableCaptionSet} captionSet
 * @param {?number} status
 * @param {function()} saveFn Gets called to reattempt save.
 */
unisubs.finishfaildialog.Dialog.show = function(captionSet, status, saveFn) {
    var dialog = new unisubs.finishfaildialog.Dialog(captionSet, status, saveFn);
    dialog.setVisible(true);
    return dialog;
};

/**
 * Called by subtitle/translate dialog if failure occurs again after 
 * calling save function again.
 * @param {?number} status Either null if no status was returned 
 *     (probably timeout), 2xx if success but bad response, and otherwise
 *     if huge potentially weird server failure.
 */
unisubs.finishfaildialog.Dialog.prototype.failedAgain = function(status) {
    if (status) {
        // it's a real error.
        this.removeChild(this.panel_, true);
        this.panel_ = new unisubs.finishfaildialog.ErrorPanel(this.captionSet_);
        this.addChild(this.panel_, true);
    }
    else
        this.panel_.showTryAgain();
};

unisubs.finishfaildialog.Dialog.prototype.createDom = function() {
    unisubs.finishfaildialog.Dialog.superClass_.createDom.call(this);
    if (this.status_)
        this.panel_ = new unisubs.finishfaildialog.ErrorPanel(this.captionSet_);
    else
        this.panel_ = new unisubs.finishfaildialog.ReattemptUploadPanel(
            this.captionSet_, this.saveFn_);
    this.addChild(this.panel_, true);
};

