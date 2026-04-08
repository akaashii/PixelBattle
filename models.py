"""
models.py — SQLAlchemy-модели (async) для PostgreSQL.
Хранит мета-данные об играх и участниках.
Холст хранится в Redis (см. canvas.py).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

from config import DATABASE_URL


# ── Engine / Session ────────────────────────────────────
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=20)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ── Enums ───────────────────────────────────────────────
class GameStatus(str, enum.Enum):
    RECRUITING = "recruiting"
    ACTIVE = "active"
    FINISHED = "finished"


# ── Models ──────────────────────────────────────────────
class Game(Base):
    __tablename__ = "games"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    status = Column(Enum(GameStatus), default=GameStatus.RECRUITING, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    started_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)

    players = relationship("Player", back_populates="game", lazy="selectin")


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False)
    username = Column(String(255), nullable=True)
    team = Column(Integer, nullable=True)  # 1 = красные, 2 = синие
    joined_at = Column(DateTime, server_default=func.now())

    game = relationship("Game", back_populates="players")


async def init_db() -> None:
    """Создаёт таблицы при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
