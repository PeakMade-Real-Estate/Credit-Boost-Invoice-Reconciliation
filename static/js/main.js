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

// Enable drag-and-drop on every file input across the app, in addition to
// the normal click-to-browse behaviour the <input type="file"> already has.
(function () {
    const fileInputs = document.querySelectorAll('input[type="file"]');
    if (!fileInputs.length) return;

    // Prevent the browser from navigating away if a file is dropped outside
    // of a recognised drop zone.
    ["dragover", "drop"].forEach((eventName) => {
        window.addEventListener(eventName, (e) => e.preventDefault());
    });

    fileInputs.forEach((input) => {
        const dropZone = input.closest(".mb-3, .mb-4") || input.parentElement;
        if (!dropZone || dropZone.dataset.dropZoneReady) return;
        dropZone.dataset.dropZoneReady = "true";
        dropZone.classList.add("file-drop-zone");

        const hint = document.createElement("div");
        hint.className = "file-drop-hint";
        hint.textContent = "or drag and drop the file here";
        input.insertAdjacentElement("afterend", hint);

        let dragCounter = 0;

        dropZone.addEventListener("dragenter", (e) => {
            e.preventDefault();
            dragCounter += 1;
            dropZone.classList.add("dragover");
        });

        dropZone.addEventListener("dragover", (e) => e.preventDefault());

        dropZone.addEventListener("dragleave", (e) => {
            e.preventDefault();
            dragCounter = Math.max(0, dragCounter - 1);
            if (dragCounter === 0) dropZone.classList.remove("dragover");
        });

        dropZone.addEventListener("drop", (e) => {
            e.preventDefault();
            dragCounter = 0;
            dropZone.classList.remove("dragover");

            const files = e.dataTransfer && e.dataTransfer.files;
            if (!files || !files.length) return;

            input.files = files;
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
    });
}());
