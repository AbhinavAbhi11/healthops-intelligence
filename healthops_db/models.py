"""SQLAlchemy models for the HealthOps multi-tenant metadata database."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


SCHEMA = "healthops"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=SCHEMA,
        naming_convention=NAMING_CONVENTION,
    )


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active",
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'archived')",
            name="status_valid",
        ),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(
        String(320), nullable=False, unique=True
    )
    display_name: Mapped[str | None] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_users_active", "is_active"),
    )


class Membership(Base):
    __tablename__ = "memberships"

    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active",
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id"], ["healthops.organizations.id"],
            ondelete="CASCADE", name="fk_memberships_organization",
        ),
        ForeignKeyConstraint(
            ["user_id"], ["healthops.users.id"],
            ondelete="CASCADE", name="fk_memberships_user",
        ),
        CheckConstraint(
            "role IN ('owner', 'admin', 'analyst', 'viewer')",
            name="role_valid",
        ),
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'removed')",
            name="status_valid",
        ),
        Index("ix_memberships_user_status", "user_id", "status"),
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active",
        server_default=text("'active'"),
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_workspaces_org_id"
        ),
        UniqueConstraint(
            "organization_id", "slug", name="uq_workspaces_org_slug"
        ),
        ForeignKeyConstraint(
            ["organization_id"], ["healthops.organizations.id"],
            ondelete="CASCADE", name="fk_workspaces_organization",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "healthops.memberships.organization_id",
                "healthops.memberships.user_id",
            ],
            ondelete="RESTRICT", name="fk_workspaces_creator_membership",
        ),
        CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="status_valid",
        ),
    )


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # Store only a reference to a secret manager entry, never credentials.
    connection_secret_ref: Mapped[str | None] = mapped_column(String(300))
    safe_config: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=text("'{}'")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active",
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "workspace_id", "id",
            name="uq_data_sources_org_workspace_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["healthops.workspaces.organization_id", "healthops.workspaces.id"],
            ondelete="CASCADE", name="fk_data_sources_workspace",
        ),
        CheckConstraint(
            "source_type IN ('upload', 'postgresql', 'mysql', 'sqlite', 'api')",
            name="source_type_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled', 'error')",
            name="status_valid",
        ),
    )


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    data_source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(500))
    file_format: Mapped[str | None] = mapped_column(String(30))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="uploaded",
        server_default=text("'uploaded'"),
    )
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    column_count: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "workspace_id", "id",
            name="uq_datasets_org_workspace_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["healthops.workspaces.organization_id", "healthops.workspaces.id"],
            ondelete="CASCADE", name="fk_datasets_workspace",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "data_source_id"],
            [
                "healthops.data_sources.organization_id",
                "healthops.data_sources.workspace_id",
                "healthops.data_sources.id",
            ],
            ondelete="RESTRICT", name="fk_datasets_data_source",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "healthops.memberships.organization_id",
                "healthops.memberships.user_id",
            ],
            ondelete="RESTRICT", name="fk_datasets_creator_membership",
        ),
        CheckConstraint(
            "status IN ('uploaded', 'profiling', 'ready', 'failed', 'archived')",
            name="status_valid",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_nonnegative"),
        CheckConstraint("row_count IS NULL OR row_count >= 0", name="rows_nonnegative"),
        CheckConstraint("column_count IS NULL OR column_count >= 0", name="columns_nonnegative"),
        Index("ix_datasets_workspace_created", "organization_id", "workspace_id", "created_at"),
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    dataset_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(700), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    column_count: Mapped[int | None] = mapped_column(Integer)
    schema_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=text("'{}'")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "workspace_id", "id",
            name="uq_dataset_versions_org_workspace_id",
        ),
        UniqueConstraint(
            "dataset_id", "version_number",
            name="uq_dataset_versions_dataset_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "dataset_id"],
            [
                "healthops.datasets.organization_id",
                "healthops.datasets.workspace_id",
                "healthops.datasets.id",
            ],
            ondelete="CASCADE", name="fk_dataset_versions_dataset",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "healthops.memberships.organization_id",
                "healthops.memberships.user_id",
            ],
            ondelete="RESTRICT", name="fk_dataset_versions_creator_membership",
        ),
        CheckConstraint("version_number > 0", name="version_positive"),
        CheckConstraint("row_count IS NULL OR row_count >= 0", name="rows_nonnegative"),
        CheckConstraint("column_count IS NULL OR column_count >= 0", name="columns_nonnegative"),
    )


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    dataset_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    requested_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued",
        server_default=text("'queued'"),
    )
    progress: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    request_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=text("'{}'")
    )
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["healthops.workspaces.organization_id", "healthops.workspaces.id"],
            ondelete="CASCADE", name="fk_analysis_jobs_workspace",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "dataset_version_id"],
            [
                "healthops.dataset_versions.organization_id",
                "healthops.dataset_versions.workspace_id",
                "healthops.dataset_versions.id",
            ],
            ondelete="RESTRICT", name="fk_analysis_jobs_dataset_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "requested_by_user_id"],
            [
                "healthops.memberships.organization_id",
                "healthops.memberships.user_id",
            ],
            ondelete="RESTRICT", name="fk_analysis_jobs_requester_membership",
        ),
        CheckConstraint(
            "job_type IN ('profile', 'quality', 'analysis', 'export', 'clean')",
            name="job_type_valid",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress_valid"),
        Index("ix_analysis_jobs_org_status_created", "organization_id", "status", "created_at"),
    )


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    dataset_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    title: Mapped[str] = mapped_column(
        String(240), nullable=False, default="New analysis"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id",
            name="uq_chat_conversations_org_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["healthops.workspaces.organization_id", "healthops.workspaces.id"],
            ondelete="CASCADE", name="fk_chat_conversations_workspace",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "dataset_id"],
            [
                "healthops.datasets.organization_id",
                "healthops.datasets.workspace_id",
                "healthops.datasets.id",
            ],
            ondelete="RESTRICT", name="fk_chat_conversations_dataset",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "healthops.memberships.organization_id",
                "healthops.memberships.user_id",
            ],
            ondelete="RESTRICT", name="fk_chat_conversations_creator_membership",
        ),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # This column accepts encrypted bytes only. Encryption is supplied by
    # the application layer before persistence.
    content_ciphertext: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False
    )
    encryption_key_ref: Mapped[str] = mapped_column(
        String(300), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            [
                "healthops.chat_conversations.organization_id",
                "healthops.chat_conversations.id",
            ],
            ondelete="CASCADE", name="fk_chat_messages_conversation",
        ),
        UniqueConstraint(
            "conversation_id", "sequence_number",
            name="uq_chat_messages_sequence",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'system', 'tool')",
            name="role_valid",
        ),
        CheckConstraint("sequence_number > 0", name="sequence_positive"),
        Index("ix_chat_messages_org_conversation", "organization_id", "conversation_id"),
    )


class SavedDashboard(Base):
    __tablename__ = "saved_dashboards"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=text("'{}'")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["healthops.workspaces.organization_id", "healthops.workspaces.id"],
            ondelete="CASCADE", name="fk_saved_dashboards_workspace",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "healthops.memberships.organization_id",
                "healthops.memberships.user_id",
            ],
            ondelete="RESTRICT", name="fk_saved_dashboards_creator_membership",
        ),
    )


class CleaningRun(Base):
    __tablename__ = "cleaning_runs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    dataset_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="planned",
        server_default=text("'planned'"),
    )
    plan_json: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=text("'{}'")
    )
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["healthops.workspaces.organization_id", "healthops.workspaces.id"],
            ondelete="CASCADE", name="fk_cleaning_runs_workspace",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "dataset_version_id"],
            [
                "healthops.dataset_versions.organization_id",
                "healthops.dataset_versions.workspace_id",
                "healthops.dataset_versions.id",
            ],
            ondelete="RESTRICT", name="fk_cleaning_runs_dataset_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            [
                "healthops.memberships.organization_id",
                "healthops.memberships.user_id",
            ],
            ondelete="RESTRICT", name="fk_cleaning_runs_creator_membership",
        ),
        CheckConstraint(
            "status IN ('planned', 'running', 'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(160))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id"], ["healthops.organizations.id"],
            ondelete="CASCADE", name="fk_audit_events_organization",
        ),
        ForeignKeyConstraint(
            ["actor_user_id"], ["healthops.users.id"],
            ondelete="SET NULL", name="fk_audit_events_actor",
        ),
        Index("ix_audit_events_org_created", "organization_id", "created_at"),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    # Persist only a SHA-256 hash of the opaque session token.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id"], ["healthops.users.id"],
            ondelete="CASCADE", name="fk_user_sessions_user",
        ),
        Index("ix_user_sessions_user_expiry", "user_id", "expires_at"),
    )
