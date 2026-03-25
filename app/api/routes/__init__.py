from app.api.routes.users import router as users_router
from app.api.routes.uploads import router as uploads_router
from app.api.routes.communities import router as communities_router
from app.api.routes.posts import router as posts_router
from app.api.routes.comments import router as comments_router

all_routers = [
    users_router,
    uploads_router,
    communities_router,
    posts_router,
    comments_router,
]