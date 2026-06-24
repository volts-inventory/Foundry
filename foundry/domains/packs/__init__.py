"""First-party domain packs (regulated / ml-data / frontend). Each pack implements
the DomainPackContract and registers into a PackRegistry by capability — added
WITHOUT core changes (the open/closed extension seam). Packs are imported directly
from their modules to avoid eager import cycles.
"""
