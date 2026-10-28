// Closes a viewer nobody is using, so the demo can go back to sleep.
//
// Fly keeps the machine awake while any page holds its connection open, and
// bills for every hour it is awake. A tab left open over a weekend would keep
// it running for the whole weekend; this lets go after IDLE_MIN minutes with
// no mouse, touch or key, and says why on the page, whose Connect button
// carries on where the visitor left off.
//
// Added to noVNC's vnc.html by the Dockerfile. `?idle_minutes=N` shortens the
// wait for testing; it cannot lengthen it.
import UI from "./app/ui.js";

const IDLE_MIN = 15;
const asked = parseFloat(new URLSearchParams(location.search).get("idle_minutes"));
const idleMs = (asked > 0 ? Math.min(asked, IDLE_MIN) : IDLE_MIN) * 60000;

let lastInput = Date.now();
for (const type of ["pointermove", "pointerdown", "wheel", "keydown", "touchstart"]) {
    // Capture, so noVNC's own handlers on the canvas cannot hide it.
    window.addEventListener(type, () => { lastInput = Date.now(); },
                            { capture: true, passive: true });
}

const note = document.createElement("p");
note.textContent = "Paused after " + Math.round(idleMs / 60000) + " minutes " +
    "without a click or key press, so the demo can go to sleep. Press " +
    "Connect to carry on.";
note.style.cssText = "max-width: 22em; margin: 0 auto 1em; color: #fff; " +
    "font: 15px/1.4 sans-serif; text-align: center;";

document.getElementById("noVNC_connect_button")
    .addEventListener("click", () => { note.remove(); lastInput = Date.now(); });

setInterval(() => {
    if (UI.connected && Date.now() - lastInput > idleMs) {
        UI.disconnect();
        document.getElementById("noVNC_connect_dlg").prepend(note);
    }
}, 10000);
