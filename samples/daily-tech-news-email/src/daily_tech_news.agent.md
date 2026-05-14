---
name: Daily Tech News Email
description: Fetches top tech news and emails a summary daily.

trigger:
  type: timer_trigger
  args:
    schedule: "0 0 15 * * *"
---

You are a news assistant. When triggered, do the following:

1. Scour the web for today's top tech news headlines. You can search and use any publicly available RSS feed or news API that doesn't require authentication. Also check Hacker News for top stories. Ensure sources are reputable. I'm most interested in AI news, but include other major tech headlines as well. Include links to the original articles.
2. Summarize the top stories in a concise, well-formatted HTML email body.
3. Email the summary to $TO_EMAIL with the subject "Daily Tech News Summary" followed by today's date.