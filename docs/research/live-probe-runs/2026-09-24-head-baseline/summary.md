# Live ATS probe

Status per field: verified = read back after blur + settle; DIDNT-STICK = reported filled but the value was gone or a placeholder after settling; FAILED = the fill itself reported failure; left = the service left it for the applicant; no-plan = scanned but not resolved.
NOTE: the probe scans EVERY frame with Playwright. The extension injects only the frames it is allowed into, so compare TOP-frame numbers for what a user gets without all-frames support.

## gh-job-boards — https://job-boards.greenhouse.io/gitlab/jobs/8556658002
- frame TOP: 21 fields {'verified': 5, 'DIDNT-STICK': 8, 'left': 8}; unseen interactive: 1; resolve 0.02s, fill 0.1s
    - DIDNT-STICK [input/text/] 'Country' <- 'United States': None (after settle '')
    - DIDNT-STICK [input/text/] 'Please choose the country in which you are located.' <- 'United States': None (after settle '')
    - DIDNT-STICK [input/text/] 'What is your current country of residence?*' <- 'United States': None (after settle '')
    - DIDNT-STICK [input/text/] 'Will you now or in the future require sponsorship for a visa to remain in your current loc' <- 'Yes': None (after settle '')
    - DIDNT-STICK [input/text/] 'Have you previously worked at or consulted for GitLab?*' <- 'No': None (after settle '')
    - DIDNT-STICK [input/text/] 'Gender' <- 'Decline to self-identify': None (after settle '')
    - DIDNT-STICK [input/text/] 'Are you Hispanic/Latino?' <- 'Decline to self-identify': None (after settle '')
    - DIDNT-STICK [input/text/] 'Veteran Status' <- 'Decline to self-identify': None (after settle '')
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 5.2s; non-GET requests blocked: 0

## gh-legacy — https://boards.greenhouse.io/faire/jobs/8746116002?gh_jid=8746116002
- frame TOP: 28 fields {'verified': 7, 'DIDNT-STICK': 3, 'left': 18}; unseen interactive: 1; resolve 0.02s, fill 0.1s
    - DIDNT-STICK [input/text/] 'Country*' <- 'United States': None (after settle '')
    - DIDNT-STICK [input/text/] 'Location (City)*' <- 'Seattle': None (after settle '')
    - DIDNT-STICK [input/text/] 'How do you identify your gender?*' <- 'Decline to self-identify': None (after settle '')
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 5.2s; non-GET requests blocked: 0

## gh-embed — https://sofi.com/careers/job/7990744003?gh_jid=7990744003
- frame TOP: 1 fields {'verified': 1}; unseen interactive: 0; resolve 0.0s, fill 0.0s
- frame https://job-boards.greenhouse.io/embed/job_app?for=sofi&validityToken=Jf13OWMlrhHIJ7GqArAjLS4Q7WpJsz9aWf5dnYGszrkuSwRBlJ: 68 fields {'verified': 12, 'DIDNT-STICK': 12, 'left': 44}; unseen interactive: 1; resolve 0.05s, fill 0.2s
    - DIDNT-STICK [input/text/] 'Country*' <- 'United States': None (after settle '')
    - DIDNT-STICK [input/text/] 'Location (City)*' <- 'Seattle': None (after settle '')
    - DIDNT-STICK [input/text/] 'Degree*' <- 'Bachelor of Design': None (after settle '')
    - DIDNT-STICK [input/text/] 'Will you now or in the future require SoFi to commence (“sponsor”) an immigration case in ' <- 'Yes': None (after settle '')
    - DIDNT-STICK [input/text/] 'Have you worked at or been a consultant for SoFi or any company subsequently acquired by a' <- 'No': None (after settle '')
    - DIDNT-STICK [input/text/] 'Are you authorized to lawfully work in the country where this role is located?*' <- 'Yes': None (after settle '')
    - DIDNT-STICK [input/text/] 'Are you currently employed with or have been employed by Deloitte? Deloitte is our externa' <- 'No': None (after settle '')
    - DIDNT-STICK [input/text/] 'Are you currently a SoFi or SoFi Tech Solutions (formerly Galileo) employee?*' <- 'No': None (after settle '')
    - DIDNT-STICK [input/text/] 'Home Address Country*' <- 'United States': None (after settle '')
    - DIDNT-STICK [input/text/] 'Gender' <- 'Decline to self-identify': None (after settle '')
    - DIDNT-STICK [input/text/] 'Race: Please select the racial category with which you most closely identify with.' <- 'Decline to self-identify': None (after settle '')
    - DIDNT-STICK [input/text/] 'Veteran Status' <- 'Decline to self-identify': None (after settle '')
    - unseen button[button] 'Toggle flyout' .icon-button icon-button--sm
- total 23.0s; non-GET requests blocked: 0

## ashby — https://jobs.ashbyhq.com/promise/0978ad8a-3098-4404-82b1-b931fc9412f9/application
- frame TOP: 40 fields {'left': 30, 'verified': 10}; unseen interactive: 6; resolve 0.02s, fill 0.0s
    - chose [radio] 'What is your gender identity?' -> 'I prefer not to answer' (intended 'I prefer not to answer', canary)
    - chose [radio] 'Do you identify as transgender?' -> 'I prefer not to answer' (intended 'I prefer not to answer', canary)
    - chose [radio] 'Input gender' -> 'Decline to self-identify' (intended 'Decline to self-identify', canary)
    - chose [radio] 'Race' -> 'Decline to self-identify' (intended 'Decline to self-identify', canary)
    - chose [radio] 'Veteran Status' -> 'I decline to self-identify for protected veteran status' (intended 'I decline to self-identify for protected veteran status', canary)
    - unseen button[] 'Are you a U.S. Citizen or Green Card holder?' ._container_pjyt6_1 _option_1svni_32  ashby-application-form-
    - unseen button[] 'Are you a U.S. Citizen or Green Card holder?' ._container_pjyt6_1 _option_1svni_32  ashby-application-form-
    - unseen button[] 'Will you now or in the future require sponsorship for employment visa status (e.g., H-1B, ' ._container_pjyt6_1 _option_1svni_32  ashby-application-form-
    - unseen button[] 'Will you now or in the future require sponsorship for employment visa status (e.g., H-1B, ' ._container_pjyt6_1 _option_1svni_32  ashby-application-form-
    - unseen button[] 'Are you able to work in person at the Promise office location listed in the job descriptio' ._container_pjyt6_1 _option_1svni_32  ashby-application-form-
    - unseen button[] 'Are you able to work in person at the Promise office location listed in the job descriptio' ._container_pjyt6_1 _option_1svni_32  ashby-application-form-
- total 8.0s; non-GET requests blocked: 10

## lever — https://jobs.lever.co/aircall/f2ab14ed-0258-4dc6-bd52-c96eb7d968e5/apply
- no fields found in any frame
- total 18.7s; non-GET requests blocked: 0
