# Live ATS probe

Status per field: verified = read back after blur + settle; DIDNT-STICK = reported filled but the value was gone or a placeholder after settling; FAILED = the fill itself reported failure; left = the service left it for the applicant; no-plan = scanned but not resolved.
NOTE: the probe scans EVERY frame with Playwright. The extension injects only the frames it is allowed into, so compare TOP-frame numbers for what a user gets without all-frames support.

## gh-job-boards — https://job-boards.greenhouse.io/gitlab/jobs/8556658002
- frame TOP: 21 fields {'verified': 13, 'left': 8}; unseen interactive: 1; resolve 0.03s, fill 0.2s
    - chose [text] 'Country' -> 'United States' (committed 'United States', intended 'United States', deterministic)
    - chose [text] 'Please choose the country in which you are located.' -> 'United States of America' (committed 'United States of America', intended 'United States', deterministic)
    - chose [text] 'What is your current country of residence?*' -> 'United States of America' (committed 'United States of America', intended 'United States', deterministic)
    - chose [text] 'Will you now or in the future require sponsorship for a visa to remain' -> 'Yes, but not one of the visas listed here' (committed 'Yes, but not one of the visas listed here', intended 'Yes, but not one of the visas listed here', canary)
    - chose [text] 'Have you previously worked at or consulted for GitLab?*' -> 'No' (committed 'No', intended 'No', answer_bank)
    - chose [text] 'Gender' -> 'Decline To Self Identify' (committed 'Decline To Self Identify', intended 'Decline to self-identify', canary)
    - chose [text] 'Are you Hispanic/Latino?' -> 'Decline To Self Identify' (committed 'Decline To Self Identify', intended 'Decline to self-identify', canary)
    - chose [text] 'Veteran Status' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 5.2s; non-GET requests blocked: 0

## gh-job-boards — https://job-boards.greenhouse.io/twilio/jobs/8177722
- frame TOP: 21 fields {'verified': 15, 'left': 6}; unseen interactive: 1; resolve 0.02s, fill 2.2s
    - chose [checkbox-group] 'How did you hear about Twilio?' -> 'LinkedIn' (committed 'LinkedIn', intended 'LinkedIn', screening)
    - chose [text] 'Country*' -> 'United States' (committed 'United States', intended 'United States', deterministic)
    - chose [text] 'Location (City)*' -> 'Seattle, Washington, United States' (committed 'Seattle, Washington, United States', intended 'Seattle, Washington', deterministic)
    - chose [text] 'Are you legally authorized to work in the United States?*' -> 'Yes' (committed 'Yes', intended 'Yes', canary)
    - chose [text] 'Will you now or in the future require sponsorship for employment visa ' -> 'Yes' (committed 'Yes', intended 'Yes', canary)
    - chose [text] 'Voluntary Self-Identification of Gender*' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - chose [text] 'Voluntary Self-Identification of Race/Ethnicity*' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - chose [text] 'Protected Veteran Status*' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - chose [text] 'Voluntary Self-Identification of Sexual Orientation*' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 7.1s; non-GET requests blocked: 0

## gh-legacy — https://boards.greenhouse.io/faire/jobs/8746116002?gh_jid=8746116002
- frame TOP: 18 fields {'verified': 10, 'left': 7, 'FAILED': 1}; unseen interactive: 1; resolve 0.02s, fill 2.1s
    - FAILED [input/checkbox-group/] 'How did you hear about Faire? (Select all that apply)' <- 'LinkedIn': no confident match among the checkbox options (saw: "Answered “unaware” to previous question", "Online advertisement (social media, display ads, etc.)", "Billboard or outdoor advertising", "Faire's website", "Faire’s technical blogs (Faire.Tech, The Craft)", "Someone I know personally (friend, family, former colleague)", "Job posting on LinkedIn, Indeed, or other job board", "LinkedIn post or content (not a job posting)") (after settle '')
    - chose [text] 'Country*' -> 'United States' (committed 'United States', intended 'United States', deterministic)
    - chose [text] 'Location (City)*' -> 'Seattle, Washington, United States' (committed 'Seattle, Washington, United States', intended 'Seattle, Washington', deterministic)
    - chose [text] 'How do you identify your gender?*' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 7.2s; non-GET requests blocked: 0

## gh-legacy — https://boards.greenhouse.io/figma/jobs/5426468004?gh_jid=5426468004
- frame TOP: 19 fields {'verified': 14, 'left': 5}; unseen interactive: 1; resolve 0.02s, fill 2.0s
    - chose [text] 'Country*' -> 'United States' (committed 'United States', intended 'United States', deterministic)
    - chose [text] 'Location (City)*' -> 'Seattle, Washington, United States' (committed 'Seattle, Washington, United States', intended 'Seattle, Washington', deterministic)
    - chose [text] 'Are you authorized to work in the country for which you applied? *' -> 'Yes' (committed 'Yes', intended 'Yes', canary)
    - chose [text] 'Have you ever worked for Figma before, as an employee or a contractor/' -> 'No' (committed 'No', intended 'No', answer_bank)
    - chose [text] 'Gender' -> 'Decline To Self Identify' (committed 'Decline To Self Identify', intended 'Decline to self-identify', canary)
    - chose [text] 'Are you Hispanic/Latino?' -> 'Decline To Self Identify' (committed 'Decline To Self Identify', intended 'Decline to self-identify', canary)
    - chose [text] 'Veteran Status' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 7.0s; non-GET requests blocked: 0

## gh-embed — https://sofi.com/careers/job/7990744003?gh_jid=7990744003
- frame TOP: 1 fields {'verified': 1}; unseen interactive: 0; resolve 0.02s, fill 0.0s
- frame https://job-boards.greenhouse.io/embed/job_app?for=sofi&validityToken=bdKyKuoXVgMRzKthhEXlTo8jMAxShn_xCIXkv01u6ECTHunwab: 35 fields {'verified': 24, 'left': 11}; unseen interactive: 1; resolve 0.02s, fill 2.5s
    - chose [text] 'Country*' -> 'United States' (committed 'United States', intended 'United States', deterministic)
    - chose [text] 'Location (City)*' -> 'Seattle, Washington, United States' (committed 'Seattle, Washington, United States', intended 'Seattle, Washington', deterministic)
    - chose [text] 'Degree*' -> "Bachelor's Degree" (committed "Bachelor's Degree", intended 'Bachelor of Design', structured)
    - chose [text] 'Will you now or in the future require SoFi to commence (“sponsor”) an ' -> 'Yes' (committed 'Yes', intended 'Yes', canary)
    - chose [text] 'Have you worked at or been a consultant for SoFi or any company subseq' -> 'No' (committed 'No', intended 'No', answer_bank)
    - chose [text] 'Are you authorized to lawfully work in the country where this role is ' -> 'Yes' (committed 'Yes', intended 'Yes', canary)
    - chose [text] 'Are you currently employed with or have been employed by Deloitte? Del' -> 'No' (committed 'No', intended 'No', answer_bank)
    - chose [text] 'Are you currently a SoFi or SoFi Tech Solutions (formerly Galileo) emp' -> 'No' (committed 'No', intended 'No', answer_bank)
    - chose [text] 'Home Address Country*' -> 'United States of America' (committed 'United States of America', intended 'United States', canary)
    - chose [text] 'Gender' -> 'Decline to Self Identify' (committed 'Decline to Self Identify', intended 'Decline to self-identify', canary)
    - chose [text] 'Race: Please select the racial category with which you most closely id' -> 'Decline to Self Identify' (committed 'Decline to Self Identify', intended 'Decline to self-identify', canary)
    - chose [text] 'Veteran Status' -> "I don't wish to answer" (committed "I don't wish to answer", intended 'Decline to self-identify', canary)
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 24.7s; non-GET requests blocked: 0

## ashby — https://jobs.ashbyhq.com/promise/0978ad8a-3098-4404-82b1-b931fc9412f9/application
- frame TOP: 26 fields {'left': 15, 'verified': 11}; unseen interactive: 0; resolve 0.01s, fill 0.1s
    - chose [radio] 'What is your gender identity?' -> 'I prefer not to answer' (committed 'I prefer not to answer', intended 'I prefer not to answer', canary)
    - chose [radio] 'Do you identify as transgender?' -> 'I prefer not to answer' (committed 'I prefer not to answer', intended 'I prefer not to answer', canary)
    - chose [radio] 'Input gender' -> 'Decline to self-identify' (committed 'Decline to self-identify', intended 'Decline to self-identify', canary)
    - chose [radio] 'Race' -> 'Decline to self-identify' (committed 'Decline to self-identify', intended 'Decline to self-identify', canary)
    - chose [radio] 'Veteran Status' -> 'I decline to self-identify for protected veteran status' (committed 'I decline to self-identify for protected veteran status', intended 'I decline to self-identify for protected veteran status', canary)
    - chose [text] 'Will you now or in the future require sponsorship for employment visa ' -> 'Yes' (committed 'Yes', intended 'Yes', canary)
- total 6.1s; non-GET requests blocked: 11

## ashby — https://jobs.ashbyhq.com/levelpath/aa97c493-a103-43ec-af52-17c798d40cf8/application
- frame TOP: 6 fields {'left': 3, 'verified': 3}; unseen interactive: 0; resolve 0.01s, fill 0.0s
- total 5.3s; non-GET requests blocked: 3

## lever — https://jobs.lever.co/aircall/f2ab14ed-0258-4dc6-bd52-c96eb7d968e5/apply
- no fields found in any frame
- total 18.7s; non-GET requests blocked: 0

## lever — https://jobs.lever.co/zerohomes/24324289-8cd5-4fa7-acf8-afcb02968991/apply
- frame TOP: 14 fields {'left': 6, 'verified': 8}; unseen interactive: 0; resolve 0.01s, fill 0.6s
    - chose [text] 'Current location' -> 'Seattle, WA, USA' (committed 'Seattle, WA, USA', intended 'Seattle, Washington', deterministic)
- total 5.5s; non-GET requests blocked: 0
