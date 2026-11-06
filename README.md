# Warning

Experimental, use at own risk, not affiliated with Ubisoft, EA, Blue Byte, Digicircus, Or any game publisher etc etc

# About

Implements online features to a local server process. Patches your Anno 2070 exe to point to a localhost server, which then exposes online functionality to you.

This is not fully tested so if you find features that don't work that should; or crashes, find out how to replicate the crash then provide me with what I need to debug it (copy of the save file, instructions on where the crash/feature is missing, and profile save if relevant)

Currently working (I think):

* Elections
* Ark storage (items and stuff)
* Profile and Career progression
* Event Quests

Not working:

* Multiplayer
* World events

# Install and Usage

Run the install, then run the run exe. 

It'll give a smartscreen warning for being an unsigned publisher. Yeah it will. I'll push a manual-install feature if you don't want to trust the built exe (although as a hint; the artifacts come from github and the code that writes the builds is also in the repo, so you have a complete chain of code -> build process code -> executables which you can audit for wrongdoing)

# Future

Multiplayer would be nice but it's a big problem since a server needs to be hosted internet or LAN accessible to coordinate lobbies. 
