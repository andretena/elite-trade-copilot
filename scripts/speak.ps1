param([Parameter(Mandatory)][string]$Text, [string]$Culture = 'en')
# Speaks text with an installed Windows voice matching the culture (e.g. pt-BR, en, es).
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $s.GetInstalledVoices() | Where-Object { $_.Enabled }
$match = $voices | Where-Object { $_.VoiceInfo.Culture.Name -eq $Culture } | Select-Object -First 1
if (-not $match) {
    $prefix = $Culture.Split('-')[0]
    $match = $voices | Where-Object { $_.VoiceInfo.Culture.Name.StartsWith($prefix) } | Select-Object -First 1
}
if ($match) { $s.SelectVoice($match.VoiceInfo.Name) }
$s.Rate = 1
$s.Speak($Text)
