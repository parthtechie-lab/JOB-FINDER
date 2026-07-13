"""Add advanced fresher fields

Revision ID: 001_advanced_fresher
Revises: 
Create Date: 2026-07-13 16:50:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001_advanced_fresher'
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Add scalar columns
    op.add_column('jobs', sa.Column('salary_min', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('salary_max', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('salary_currency', sa.String(length=3), nullable=True))
    op.add_column('jobs', sa.Column('salary_period', sa.String(length=10), nullable=True))
    op.add_column('jobs', sa.Column('years_of_experience', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('alerted_at', sa.DateTime(), nullable=True))
    
    # Add JSON and ARRAY columns
    op.add_column('jobs', sa.Column('compensation_breakdown', postgresql.JSON(astext_type=sa.Text()), nullable=True))
    op.add_column('jobs', sa.Column('certifications_required', postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column('jobs', sa.Column('certifications_preferred', postgresql.ARRAY(sa.String()), nullable=True))
    
    # Create indexes
    op.create_index('idx_score_alerted_at', 'jobs', ['score', 'alerted_at'], unique=False)

def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_score_alerted_at', table_name='jobs')
    
    # Drop columns
    op.drop_column('jobs', 'certifications_preferred')
    op.drop_column('jobs', 'certifications_required')
    op.drop_column('jobs', 'compensation_breakdown')
    op.drop_column('jobs', 'alerted_at')
    op.drop_column('jobs', 'years_of_experience')
    op.drop_column('jobs', 'salary_period')
    op.drop_column('jobs', 'salary_currency')
    op.drop_column('jobs', 'salary_max')
    op.drop_column('jobs', 'salary_min')
