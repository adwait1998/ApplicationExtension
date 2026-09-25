# Live ATS probe

Status per field: verified = read back after blur + settle; DIDNT-STICK = reported filled but the value was gone or a placeholder after settling; FAILED = the fill itself reported failure; left = the service left it for the applicant; no-plan = scanned but not resolved.
NOTE: the probe scans EVERY frame with Playwright. The extension injects only the frames it is allowed into, so compare TOP-frame numbers for what a user gets without all-frames support.

## gh-job-boards — https://job-boards.greenhouse.io/gitlab/jobs/8556658002
- frame TOP: 21 fields {'verified': 11, 'left': 8, 'FAILED': 2}; unseen interactive: 2; resolve 0.04s, fill 1.2s
    - FAILED [input/text/combobox] 'Country' <- 'United States': selection did not commit (search text may have been dropped on blur) (after settle '')
    - chose [text] 'Please choose the country in which you are located.' -> 'United States of America' (intended 'United States', deterministic)
    - chose [text] 'What is your current country of residence?*' -> 'United States of America' (intended 'United States', deterministic)
    - FAILED [input/text/combobox] 'Will you now or in the future require sponsorship for a visa to remain in your current loc' <- 'Yes': no confident match for "Yes" among filtered options (after settle '')
    - chose [text] 'Have you previously worked at or consulted for GitLab?*' -> 'No' (intended 'No', answer_bank)
    - chose [text] 'Gender' -> 'Decline To Self Identify' (intended 'Decline to self-identify', canary)
    - chose [text] 'Are you Hispanic/Latino?' -> 'Decline To Self Identify' (intended 'Decline to self-identify', canary)
    - chose [text] 'Veteran Status' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
    - unseen div[listbox] 'Select...' .select__menu-list remix-css-qr46ko
- total 8.9s; non-GET requests blocked: 0

## gh-job-boards — https://job-boards.greenhouse.io/twilio/jobs/8177722
- frame TOP: 21 fields {'verified': 13, 'left': 6, 'FAILED': 2}; unseen interactive: 2; resolve 0.02s, fill 3.2s
    - chose [checkbox-group] 'How did you hear about Twilio?' -> 'LinkedIn' (intended 'LinkedIn', screening)
    - FAILED [input/text/combobox] 'Country*' <- 'United States': selection did not commit (search text may have been dropped on blur) (after settle '')
    - FAILED [input/text/combobox] 'Location (City)*' <- 'Seattle': no confident match for "Seattle" among filtered options (after settle '')
    - chose [text] 'Are you legally authorized to work in the United States?*' -> 'Yes' (intended 'Yes', canary)
    - chose [text] 'Will you now or in the future require sponsorship for employment visa ' -> 'Yes' (intended 'Yes', canary)
    - chose [text] 'Voluntary Self-Identification of Gender*' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - chose [text] 'Voluntary Self-Identification of Race/Ethnicity*' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - chose [text] 'Protected Veteran Status*' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - chose [text] 'Voluntary Self-Identification of Sexual Orientation*' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
    - unseen div[listbox] 'Location (City)*' .select__menu-list remix-css-qr46ko
- total 8.0s; non-GET requests blocked: 0

## gh-legacy — https://boards.greenhouse.io/faire/jobs/8746116002?gh_jid=8746116002
- frame TOP: 18 fields {'verified': 8, 'left': 7, 'FAILED': 3}; unseen interactive: 2; resolve 0.03s, fill 3.0s
    - FAILED [input/checkbox-group/] 'How did you hear about Faire? (Select all that apply)' <- 'LinkedIn': no confident match for "LinkedIn" among checkbox options (after settle '')
    - FAILED [input/text/combobox] 'Country*' <- 'United States': selection did not commit (search text may have been dropped on blur) (after settle '')
    - FAILED [input/text/combobox] 'Location (City)*' <- 'Seattle': no confident match for "Seattle" among filtered options (after settle '')
    - chose [text] 'How do you identify your gender?*' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
    - unseen div[listbox] 'Location (City)*' .select__menu-list remix-css-qr46ko
- total 8.1s; non-GET requests blocked: 0

## gh-legacy — https://boards.greenhouse.io/figma/jobs/5426468004?gh_jid=5426468004
- frame TOP: 19 fields {'verified': 12, 'left': 5, 'FAILED': 2}; unseen interactive: 2; resolve 0.01s, fill 3.1s
    - FAILED [input/text/combobox] 'Country*' <- 'United States': selection did not commit (search text may have been dropped on blur) (after settle '')
    - FAILED [input/text/combobox] 'Location (City)*' <- 'Seattle': no confident match for "Seattle" among filtered options (after settle '')
    - chose [text] 'Are you authorized to work in the country for which you applied? *' -> 'Yes' (intended 'Yes', canary)
    - chose [text] 'Have you ever worked for Figma before, as an employee or a contractor/' -> 'No' (intended 'No', answer_bank)
    - chose [text] 'Gender' -> 'Decline To Self Identify' (intended 'Decline to self-identify', canary)
    - chose [text] 'Are you Hispanic/Latino?' -> 'Decline To Self Identify' (intended 'Decline to self-identify', canary)
    - chose [text] 'Veteran Status' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
    - unseen div[listbox] 'Location (City)*' .select__menu-list remix-css-qr46ko
- total 8.0s; non-GET requests blocked: 0

## gh-embed — https://sofi.com/careers/job/7990744003?gh_jid=7990744003
- frame TOP: 1 fields {'verified': 1}; unseen interactive: 0; resolve 0.02s, fill 0.0s
- frame https://job-boards.greenhouse.io/embed/job_app?for=sofi&validityToken=T0C8IjwTkxtdURoiCN90WQZEZCeLawaQAbG6q2xk4or7kp5-01: 35 fields {'verified': 22, 'left': 11, 'FAILED': 2}; unseen interactive: 2; resolve 0.03s, fill 3.3s
    - FAILED [input/text/combobox] 'Country*' <- 'United States': selection did not commit (search text may have been dropped on blur) (after settle '')
    - FAILED [input/text/combobox] 'Location (City)*' <- 'Seattle': no confident match for "Seattle" among filtered options (after settle '')
    - chose [text] 'Degree*' -> "Bachelor's Degree" (intended 'Bachelor of Design', structured)
    - chose [text] 'Will you now or in the future require SoFi to commence (“sponsor”) an ' -> 'Yes' (intended 'Yes', canary)
    - chose [text] 'Have you worked at or been a consultant for SoFi or any company subseq' -> 'No' (intended 'No', answer_bank)
    - chose [text] 'Are you authorized to lawfully work in the country where this role is ' -> 'Yes' (intended 'Yes', canary)
    - chose [text] 'Are you currently employed with or have been employed by Deloitte? Del' -> 'No' (intended 'No', answer_bank)
    - chose [text] 'Are you currently a SoFi or SoFi Tech Solutions (formerly Galileo) emp' -> 'No' (intended 'No', answer_bank)
    - chose [text] 'Home Address Country*' -> 'United States of America' (intended 'United States', canary)
    - chose [text] 'Gender' -> 'Decline to Self Identify' (intended 'Decline to self-identify', canary)
    - chose [text] 'Race: Please select the racial category with which you most closely id' -> 'Decline to Self Identify' (intended 'Decline to self-identify', canary)
    - chose [text] 'Veteran Status' -> "I don't wish to answer" (intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
    - unseen div[listbox] 'Location (City)*' .select__menu-list remix-css-qr46ko
- total 25.5s; non-GET requests blocked: 0

## ashby — https://jobs.ashbyhq.com/promise/0978ad8a-3098-4404-82b1-b931fc9412f9/application
- frame TOP: 26 fields {'left': 15, 'verified': 11}; unseen interactive: 0; resolve 0.02s, fill 0.1s
    - chose [radio] 'What is your gender identity?' -> 'I prefer not to answer' (intended 'I prefer not to answer', canary)
    - chose [radio] 'Do you identify as transgender?' -> 'I prefer not to answer' (intended 'I prefer not to answer', canary)
    - chose [radio] 'Input gender' -> 'Decline to self-identify' (intended 'Decline to self-identify', canary)
    - chose [radio] 'Race' -> 'Decline to self-identify' (intended 'Decline to self-identify', canary)
    - chose [radio] 'Veteran Status' -> 'I decline to self-identify for protected veteran status' (intended 'I decline to self-identify for protected veteran status', canary)
    - chose [text] 'Will you now or in the future require sponsorship for employment visa ' -> 'Yes' (intended 'Yes', canary)
- total 6.4s; non-GET requests blocked: 11

## ashby — https://jobs.ashbyhq.com/levelpath/aa97c493-a103-43ec-af52-17c798d40cf8/application
- frame TOP: 6 fields {'left': 3, 'verified': 3}; unseen interactive: 0; resolve 0.03s, fill 0.0s
- total 5.8s; non-GET requests blocked: 3

## lever — https://jobs.lever.co/aircall/f2ab14ed-0258-4dc6-bd52-c96eb7d968e5/apply
- no fields found in any frame
- total 18.7s; non-GET requests blocked: 0

## lever — https://jobs.lever.co/zerohomes/24324289-8cd5-4fa7-acf8-afcb02968991/apply
- frame TOP: 14 fields {'left': 6, 'verified': 8}; unseen interactive: 0; resolve 0.03s, fill 0.6s
    - chose [text] 'Current location' -> '{"name":"Seattle, WA, USA","id":"f93b25a37ff88c0b43e96bb6621c6a83f1914611"}' (intended 'Seattle', deterministic)
- total 5.5s; non-GET requests blocked: 0
