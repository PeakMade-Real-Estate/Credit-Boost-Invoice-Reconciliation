/* Invoice Reconciliation – main.js
 *
 * Lightweight client-side helpers only.
 * No business logic lives here.
 */

"use strict";

// Confirm navigation away from an unsaved exception-resolution form
(function () {
    const form = document.getElementById("exceptionsForm");
    if (!form) return;

    let formChanged = false;

    form.addEventListener("change", () => { formChanged = true; });
    form.addEventListener("submit", () => { formChanged = false; });

    window.addEventListener("beforeunload", (e) => {
        if (formChanged) {
            e.preventDefault();
            e.returnValue = "";
        }
    });
}());
