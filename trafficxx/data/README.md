# Passphrase word list

`eff_large_wordlist.txt` is EFF's unmodified 7,776-entry Long Wordlist,
downloaded from https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt.
Copyright Electronic Frontier Foundation, under CC BY 3.0 US:
https://creativecommons.org/licenses/by/3.0/us/.
Source and attribution: https://www.eff.org/dice.

ClickClash selects five words independently using Python's cryptographically
secure `secrets.choice`, with online login rate limits and Argon2id hashes.
This is the product's five-word format, not a claim to meet EFF's general
recommendation of six words. Never substitute a smaller informal word list.
