ApplyPilot Copilot - setup
==========================

ApplyPilot Copilot fills job applications in Chrome for you. You review every
answer and press Submit yourself; it never submits anything.
Everything stays on your computer.

You need: Google Chrome (version 138 or newer).


1. Run the installer
--------------------

Windows: double-click ApplyPilotCopilot-Setup.exe.
  Windows may say "Windows protected your PC". Click "More info", then
  "Run anyway". (It says this because the app isn't signed by a big company.)

Mac: unzip the download, then right-click "Install ApplyPilot Copilot.command"
  and choose Open, then Open again. (macOS asks because it isn't from the App
  Store.) A Terminal window shows the progress; close it when it says Done.


2. Add the extension to Chrome (one time)
-----------------------------------------

1. In Chrome, go to:  chrome://extensions
2. Turn on "Developer mode" (switch at the top right).
3. Click "Load unpacked" and choose the extension folder:
   Windows: in the folder box, paste  %LOCALAPPDATA%\Programs\ApplyPilotCopilot\extension
            and press Enter, then click "Select Folder".
   Mac:     Home > Applications > ApplyPilot Copilot > extension, then "Select".
4. Click the puzzle-piece icon in Chrome's toolbar and pin ApplyPilot Copilot.

Chrome may show a banner about developer-mode extensions. That's expected;
you can dismiss it.


3. Set up your profile
----------------------

1. Right-click the ApplyPilot icon > Options.
2. Under "Import your résumé", choose your résumé (PDF, Word or text file).
3. Check what it found, fill anything missing, and save.


4. Optional: turn on on-device AI
---------------------------------

AI drafts answers to open-ended questions and reads your work history from
your résumé. It runs on your own computer using Chrome's built-in AI, so
nothing is sent anywhere.

In Options > Smart fill, click "Download on-device model" (a one-time download
of a few GB). If it says "not available here", your computer doesn't meet
Chrome's requirements (about 22 GB of free disk space, and a graphics card
with more than 4 GB of memory or 16 GB of RAM). Everything else still works.

Keep the ApplyPilot side panel open while it fills, so the AI can answer.


5. Use it
---------

1. Open a job application page.
2. Click the ApplyPilot icon: the side panel opens.
3. Click "Fill this page". The first time on each website, Chrome asks for
   permission: click Allow.
4. Review every field (green = filled, amber = left for you), then submit it
   yourself.


Not in this version
-------------------

- Tailored résumés (the Tailor button says it isn't available).


Removing it
-----------

Windows: Settings > Apps > ApplyPilot Copilot > Uninstall.
Mac: open Home > Applications > ApplyPilot Copilot and double-click
  "Uninstall ApplyPilot Copilot.command" (it's also in the download folder).
Then remove the extension in chrome://extensions.
Your profile and saved answers stay in the ".applypilot" folder in your home
folder; delete that folder to remove them too.
