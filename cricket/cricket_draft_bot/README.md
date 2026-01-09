# Cricket Draft & Comparison Bot

A Telegram bot for drafting cricket teams and comparing stats.

## Setup

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
2.  Add your Telegram Bot Token in `config.py`.
3.  Run the bot:
    ```bash
    python main.py
    ```

## Admin Commands

### Add Player
Add a new player to the database.

**Syntax:**
`/add_player name=<Name> roles=<Role1>,<Role2> image=<ImageURL>`

**Example:**
```
/add_player name=Rohit Sharma roles=Captain,Hitting image=https://example.com/rohit.jpg
```

**Notes:**
- `roles` must be comma-separated.
- `image` must be a direct URL to an image.
- The bot will generate a standard ID (e.g., `IND_ROHIT`) automatically.

### Map API Stats
Link a player to external API IDs for stats fetching (optional).

**Syntax:**
`/map_api player_id=<PlayerID> ipl_id=<IPL_ID> international_id=<Intl_ID>`

**Example:**
```
/map_api player_id=IND_ROHIT ipl_id=35320 international_id=12988
```

## Game Commands

- `/challenge_ipl @username`: Challenge a user in IPL mode.
- `/challenge_intl @username`: Challenge a user in International mode.
