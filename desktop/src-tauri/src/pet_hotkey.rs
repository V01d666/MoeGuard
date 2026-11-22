//! Configurable Win32 shortcut; no keyboard hook or WebView is required.
#[derive(PartialEq, Eq)]
struct Shortcut {
    modifiers: u32,
    key: u32,
    label: String,
}

impl Shortcut {
    fn parse(text: &str) -> Result<Option<Self>, &'static str> {
        const INVALID: &str = "pet_hotkey_invalid";
        if text.len() > 48 || !text.is_ascii() {
            return Err(INVALID);
        }
        if text.trim().is_empty() {
            return Ok(None);
        }
        // These values are the Win32 HOT_KEY_MODIFIERS ABI.
        let names = [("Ctrl", 2), ("Alt", 1), ("Shift", 4), ("Win", 8)];
        let mut parts = text.split('+').map(str::trim);
        let key = parts.next_back().ok_or(INVALID)?.to_ascii_uppercase();
        let mut modifiers = 0;
        for part in parts {
            let (_, bit) = names
                .iter()
                .find(|(name, _)| part.eq_ignore_ascii_case(name))
                .ok_or(INVALID)?;
            if modifiers & bit != 0 {
                return Err(INVALID);
            }
            modifiers |= bit;
        }
        if modifiers & (1 | 2 | 8) == 0 {
            return Err(INVALID);
        }
        let vk = match key.as_bytes() {
            [b @ (b'A'..=b'Z' | b'0'..=b'9')] => u32::from(*b),
            _ => {
                let number = key
                    .strip_prefix('F')
                    .and_then(|part| part.parse::<u32>().ok())
                    .ok_or(INVALID)?;
                if !(1..=11).contains(&number) || key != format!("F{number}") {
                    return Err(INVALID);
                }
                0x6f + number
            }
        };
        let mut label = String::new();
        for (name, bit) in names {
            if modifiers & bit != 0 {
                label.push_str(name);
                label.push('+');
            }
        }
        label.push_str(&key);
        Ok(Some(Self {
            modifiers,
            key: vk,
            label,
        }))
    }
}

#[cfg(all(windows, feature = "pet-win32"))]
#[path = "pet_hotkey_win32.rs"]
pub(crate) mod native;

#[cfg(test)]
mod tests {
    use super::Shortcut;

    #[test]
    fn parses_supported_chords_without_registering_bare_or_reserved_keys() {
        let chord = Shortcut::parse("shift + CTRL + h").unwrap().unwrap();
        assert_eq!(chord.label, "Ctrl+Shift+H");
        assert_eq!((chord.modifiers, chord.key), (6, u32::from(b'H')));
        assert_eq!(Shortcut::parse("Alt+F11").unwrap().unwrap().key, 0x7a);
        assert!(Shortcut::parse("").unwrap().is_none());
        for text in [
            "H",
            "Shift+H",
            "Ctrl+Ctrl+H",
            "Ctrl+F12",
            "Ctrl+Alt+Delete",
            "Ctrl+",
        ] {
            assert!(Shortcut::parse(text).is_err(), "{text}");
        }
    }
}
