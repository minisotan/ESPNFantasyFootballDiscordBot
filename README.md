This is an app that keeps track of your Fantasy Football League!

In order to use it properly you need to get your: LEAGUE ID, SWID KEY, and ESPN_S2 KEY.

1. To find your LEAGUE_ID
Log in to Your ESPN Fantasy Football Account:

Look at the URL in your browser's address bar. It usually looks something like this:
https://fantasy.espn.com/football/league?leagueId=1234567

Copy the numbers at the end. 
Paste into the credentials.py file

2. Finding the SWID
Inspect Your Browser's Developer Tools:

Right-click anywhere on the ESPN Fantasy Football page and select "Inspect" or "Inspect Element."

In the left sidebar, expand the "Cookies" section and select espn.com.
Look for a cookie named SWID. The value associated with this cookie is your SWID. It looks something like this:

{12345ABC-6789-DEF0-1234-56789ABCDEF0}

Copy the value and paste it into the credentials.py file

3. Finding the ESPN_S2
Check the Same Cookie Storage:

While still in the "Cookies" section under espn.com, look for a cookie named espn_s2.

Copy the value and paste it into the credentials.py file

The value of this cookie will be your ESPN_S2. It might look like a long string similar to this:

AEAotuzI3MCxskhbUE7lsjVTLPhoyUT%2FYwPU5kXxbwBQgc8gwSWf68DOQHh%2FnWwopXNb2mJRAzed3%2BfYWgtkvnHeSTnZsN7YPhRUHAqXAOpnuFL4SQTalw3zpXexT2LGg2gClRsfRppCtyxjS7Tp8hO7mz4AjitpRGX92WCSPQ1iWwpVv4gP9kp45l9UiD76oEHEEpsayOsj%2BzXdQcQTkgEzWUhGw0HMKYwLDm9puDKiTnYH5cyOhQN3oxGJj%2B8Fa1R%2FYFuNpmGGQF3%2BiZkgHo%2FdUtPeh%2BB8MIdx0r3%2BdxAIAAxIYSjaB%2FJKkghZ3IZ4xro%3D