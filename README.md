# suno-midi-tidy
Suno Studio、Basic Pitch、Melodyne 转出的 MIDI 通病是：程序能打开，一进 DAW 或 MuseScore 就发现没法用——力度是假的、音符是多的、时间轴是错的。 本技能只做一件事，但做得彻底：不采信 MIDI 里写的任何数字，一切回到同一条分轨的 WAV 里找证据。 - 力度重建：丢掉原始 Velocity，用每个音符起点后 90ms 窗口的真实音量重算强弱 - 起音对齐
