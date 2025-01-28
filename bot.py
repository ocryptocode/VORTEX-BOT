import os
import discord
from discord.ext import commands
from discord import Intents
import random
from dotenv import load_dotenv
import asyncio
from discord.ext import tasks
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import redis
import traceback
import logging
from database import DatabaseManager

# Setup logger
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
print(f"Token loaded: {DISCORD_BOT_TOKEN[:10]}...") # Only prints first 10 chars for safety

if not DISCORD_BOT_TOKEN:
    raise ValueError("No Discord token found. Make sure DISCORD_BOT_TOKEN is set in your .env file")

print("Environment variables:")
print(f"Token type: {type(DISCORD_BOT_TOKEN)}")
print(f"Token length: {len(DISCORD_BOT_TOKEN) if DISCORD_BOT_TOKEN else 'None'}")
print(f"Token first 10 chars: {DISCORD_BOT_TOKEN[:10] if DISCORD_BOT_TOKEN else 'None'}")

class VortexBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        
        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=commands.DefaultHelpCommand()
        )
        
        # Initialize core systems
        self.db = DatabaseManager()
        self.redis = redis.Redis(
            host='localhost',
            port=6379,
            db=0,
            decode_responses=True
        )
        
        # Quiz system variables
        self.active_quizzes = {}
        self.quiz_cooldowns = {}
        self.quiz_rewards = {
            'easy': 5,      # 5 vortex_coins
            'medium': 10,   # 10 vortex_coins
            'hard': 20      # 20 vortex_coins
        }
        
        # Social farming variables
        self.farming_sessions = {}
        self.farming_cooldowns = {}
        self.farming_rewards = {
            'message': 1,    # 1 vortex_coin per valid message
            'reaction': 0.5, # 0.5 vortex_coins per reaction
            'daily_cap': 50  # Maximum 50 vortex_coins per day
        }
        
        # Mining System
        self.mining_sessions = {}
        self.mining_rewards = {
            'basic': 1,     # 1 vortex_coin per minute
            'rare': 5,      # 5 vortex_coins (10% chance)
            'epic': 10      # 10 vortex_coins (5% chance)
        }
        self.mining_cooldowns = {}
        
        # Game System
        self.active_games = {}
        self.game_rewards = {
            'win': 10,
            'participate': 2
        }
        
        # Governance System
        self.active_proposals = {}
        self.voting_power = {}  # Based on user's vortex_coins
        
        # Airdrop System
        self.scheduled_airdrops = []
        self.airdrop_participants = set()
        
        # Payment System
        self.pending_payments = {}
        self.payment_history = {}

    async def setup_hook(self):
        """Load all cogs when bot starts"""
        # Load utility cogs
        await self.load_extension("cogs.error_handler")
        await self.load_extension("cogs.events")
        
        # Load feature cogs
        COGS = [
            "cogs.game",         # Game mechanics
            "cogs.raids",        # Raid rewards
            "cogs.referrals"     # Referral program
        ]
        
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info(f"Loaded extension: {cog}")
            except Exception as e:
                logger.error(f"Failed to load extension {cog}: {e}")

    async def on_ready(self):
        logger.info(f'{self.user} has connected to Discord!')
        logger.info(f'Connected to {len(self.guilds)} guilds')
        logger.info(f'Bot latency: {round(self.latency * 1000)}ms')

    # Quiz System Commands
    @commands.command()
    @commands.cooldown(1, 300, commands.BucketType.user)  # 5 minute cooldown
    async def quiz(self, ctx, difficulty='easy'):
        """Start a quiz with specified difficulty"""
        if ctx.author.id in self.active_quizzes:
            await ctx.send("You already have an active quiz!")
            return

        questions = await self.db.get_quiz_questions(difficulty)
        if not questions:
            await ctx.send("No questions available for this difficulty!")
            return

        question = random.choice(questions)
        self.active_quizzes[ctx.author.id] = {
            'question': question,
            'attempts': 0,
            'max_attempts': 3
        }

        embed = discord.Embed(
            title="Vortex Quiz",
            description=f"**{question['question']}**\nReward: {self.quiz_rewards[difficulty]} vortex_coins",
            color=0x5865F2
        )
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        # Handle quiz answers
        if message.author.id in self.active_quizzes:
            quiz = self.active_quizzes[message.author.id]
            if message.content.lower() == quiz['question']['answer'].lower():
                reward = self.quiz_rewards[quiz['question']['difficulty']]
                await self.db.add_balance(message.author.id, reward)
                await message.channel.send(
                    f"🎉 Correct! You earned {reward} vortex_coins!"
                )
                del self.active_quizzes[message.author.id]
            else:
                quiz['attempts'] += 1
                if quiz['attempts'] >= quiz['max_attempts']:
                    await message.channel.send("❌ Out of attempts! The correct answer was: " + quiz['question']['answer'])
                    del self.active_quizzes[message.author.id]
                else:
                    await message.channel.send(f"❌ Wrong answer! {quiz['max_attempts'] - quiz['attempts']} attempts remaining.")

        # Handle social farming
        await self._process_social_farming(message)

        # Handle game guesses
        if message.author.id in self.active_games:
            try:
                guess = int(message.content)
                game = self.active_games[message.author.id]
                game['attempts'] += 1
                
                if guess == game['number']:
                    await self.db.add_balance(message.author.id, self.game_rewards['win'])
                    await message.channel.send(f"🎉 Correct! You won {self.game_rewards['win']} vortex_coins!")
                    del self.active_games[message.author.id]
                elif game['attempts'] >= game['max_attempts']:
                    await self.db.add_balance(message.author.id, self.game_rewards['participate'])
                    await message.channel.send(f"Game Over! The number was {game['number']}. You got {self.game_rewards['participate']} vortex_coins for participating!")
                    del self.active_games[message.author.id]
                else:
                    hint = "higher" if guess < game['number'] else "lower"
                    await message.channel.send(f"Wrong! The number is {hint}. {game['max_attempts'] - game['attempts']} attempts left.")
            except ValueError:
                pass

    async def _process_social_farming(self, message):
        """Process social farming rewards for valid messages"""
        user_id = message.author.id
        
        # Check daily cap
        daily_earnings = await self.db.get_daily_earnings(user_id)
        if daily_earnings >= self.farming_rewards['daily_cap']:
            return

        # Check if message is valid for farming
        if len(message.content) >= 10 and not message.content.startswith('!'):
            # Check cooldown
            last_reward = self.farming_cooldowns.get(user_id, 0)
            if time.time() - last_reward >= 60:  # 1 minute cooldown
                reward = self.farming_rewards['message']
                await self.db.add_balance(user_id, reward)
                self.farming_cooldowns[user_id] = time.time()
                
                # Random chance for bonus
                if random.random() < 0.1:  # 10% chance
                    bonus = reward * 2
                    await self.db.add_balance(user_id, bonus)
                    await message.add_reaction('🎉')
                    await message.channel.send(
                        f"🎉 Bonus! {message.author.mention} earned {bonus} extra vortex_coins!"
                    )

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        """Handle reaction farming rewards"""
        if user.bot:
            return

        # Check daily cap
        daily_earnings = await self.db.get_daily_earnings(user.id)
        if daily_earnings >= self.farming_rewards['daily_cap']:
            return

        # Award small reward for reactions
        reward = self.farming_rewards['reaction']
        await self.db.add_balance(user.id, reward)

    @commands.command()
    async def balance(self, ctx):
        """Check your vortex_coins balance"""
        balance = await self.db.get_balance(ctx.author.id)
        embed = discord.Embed(
            title="Vortex Wallet",
            description=f"Balance: {balance} vortex_coins",
            color=0x5865F2
        )
        await ctx.send(embed=embed)

    # Mining System
    @commands.command()
    @commands.cooldown(1, 3600, commands.BucketType.user)  # 1 hour cooldown
    async def mine(self, ctx):
        """Start mining vortex_coins"""
        if ctx.author.id in self.mining_sessions:
            await ctx.send("You're already mining!")
            return
            
        self.mining_sessions[ctx.author.id] = {
            'start_time': time.time(),
            'duration': 300  # 5 minutes mining session
        }
        
        embed = discord.Embed(
            title="⛏️ Mining Started",
            description="Mining session started for 5 minutes!",
            color=0x00ff00
        )
        await ctx.send(embed=embed)
        
        # Start mining session
        await asyncio.sleep(300)
        
        if ctx.author.id in self.mining_sessions:
            # Calculate rewards
            base_reward = self.mining_rewards['basic'] * 5
            bonus = 0
            
            # Random bonuses
            if random.random() < 0.1:  # 10% chance
                bonus += self.mining_rewards['rare']
            if random.random() < 0.05:  # 5% chance
                bonus += self.mining_rewards['epic']
                
            total_reward = base_reward + bonus
            await self.db.add_balance(ctx.author.id, total_reward)
            
            embed = discord.Embed(
                title="⛏️ Mining Complete",
                description=f"You earned {total_reward} vortex_coins! ({bonus} bonus)",
                color=0x00ff00
            )
            await ctx.send(embed=embed)
            del self.mining_sessions[ctx.author.id]

    # Game System
    @commands.command()
    async def play(self, ctx):
        """Start a simple number guessing game"""
        if ctx.author.id in self.active_games:
            await ctx.send("You're already in a game!")
            return
            
        number = random.randint(1, 100)
        self.active_games[ctx.author.id] = {
            'number': number,
            'attempts': 0,
            'max_attempts': 5
        }
        
        await ctx.send("I'm thinking of a number between 1 and 100. You have 5 attempts!")

    # Governance System
    @commands.command()
    async def propose(self, ctx, *, proposal):
        """Create a governance proposal"""
        user_balance = await self.db.get_balance(ctx.author.id)
        if user_balance < 100:  # Minimum 100 coins to create proposal
            await ctx.send("You need at least 100 vortex_coins to create a proposal!")
            return
            
        proposal_id = len(self.active_proposals)
        self.active_proposals[proposal_id] = {
            'text': proposal,
            'creator': ctx.author.id,
            'votes_for': 0,
            'votes_against': 0,
            'voters': set(),
            'end_time': time.time() + 86400  # 24 hours
        }
        
        embed = discord.Embed(
            title=f"Proposal #{proposal_id}",
            description=proposal,
            color=0x5865F2
        )
        msg = await ctx.send(embed=embed)
        await msg.add_reaction('👍')
        await msg.add_reaction('👎')

    # Airdrop System
    @commands.command()
    @commands.has_permissions(administrator=True)
    async def airdrop(self, ctx, amount: int, duration: int):
        """Start an airdrop event"""
        self.airdrop_participants.clear()
        
        embed = discord.Embed(
            title="🎁 Airdrop Started!",
            description=f"React with 🎁 to participate! {amount} vortex_coins will be distributed in {duration} minutes!",
            color=0x5865F2
        )
        msg = await ctx.send(embed=embed)
        await msg.add_reaction('🎁')
        
        await asyncio.sleep(duration * 60)
        
        if self.airdrop_participants:
            coins_per_user = amount // len(self.airdrop_participants)
            for user_id in self.airdrop_participants:
                await self.db.add_balance(user_id, coins_per_user)
            
            await ctx.send(f"Airdrop complete! {coins_per_user} vortex_coins distributed to {len(self.airdrop_participants)} participants!")

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot:
            return
            
        # Handle airdrop participation
        if str(reaction.emoji) == '🎁':
            self.airdrop_participants.add(user.id)
            
        # Handle governance voting
        if str(reaction.emoji) in ['👍', '👎']:
            for proposal_id, proposal in self.active_proposals.items():
                if reaction.message.id == proposal.get('message_id'):
                    if user.id not in proposal['voters']:
                        voting_power = await self.db.get_balance(user.id)
                        if str(reaction.emoji) == '👍':
                            proposal['votes_for'] += voting_power
                        else:
                            proposal['votes_against'] += voting_power
                        proposal['voters'].add(user.id)

if __name__ == "__main__":
    bot = VortexBot()
    
    try:
        print("Starting bot...")
        print(f"Token loaded: {DISCORD_BOT_TOKEN[:10]}...")  # Only prints first 10 chars
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        logger.critical(f"Bot failed to start: {e}\n{traceback.format_exc()}")
