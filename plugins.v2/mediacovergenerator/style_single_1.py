import base64
import random
import colorsys
import math
import os
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Tuple, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageEnhance

from app.log import logger


# ========== 配置 ==========
canvas_size = (1920, 1080)

def is_not_black_white_gray_near(color, threshold=20):
    """判断颜色既不是黑、白、灰，也不是接近黑、白。"""
    r, g, b = color
    if (r < threshold and g < threshold and b < threshold) or \
       (r > 255 - threshold and g > 255 - threshold and b > 255 - threshold):
        return False
    gray_diff_threshold = 10
    if abs(r - g) < gray_diff_threshold and abs(g - b) < gray_diff_threshold and abs(r - b) < gray_diff_threshold:
        return False
    return True

def rgb_to_hsv(color):
    """将 RGB 颜色转换为 HSV 颜色。"""
    r, g, b = [x / 255.0 for x in color]
    return colorsys.rgb_to_hsv(r, g, b)

def hsv_to_rgb(h, s, v):
    """将 HSV 颜色转换为 RGB 颜色。"""
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))

def adjust_color_macaron(color):
    """
    调整颜色使其更接近马卡龙风格：
    - 如果颜色太暗，增加亮度
    - 如果颜色太亮，降低亮度
    - 调整饱和度到适当范围
    """
    h, s, v = rgb_to_hsv(color)
    
    # 马卡龙风格的理想范围
    target_saturation_range = (0.3, 0.7)  # 饱和度范围
    target_value_range = (0.6, 0.85)      # 亮度范围
    
    # 调整饱和度
    if s < target_saturation_range[0]:
        s = target_saturation_range[0]
    elif s > target_saturation_range[1]:
        s = target_saturation_range[1]
    
    # 调整亮度
    if v < target_value_range[0]:
        v = target_value_range[0]  # 太暗，加亮
    elif v > target_value_range[1]:
        v = target_value_range[1]  # 太亮，加暗
    
    return hsv_to_rgb(h, s, v)

def color_distance(color1, color2):
    """计算两个颜色在HSV空间中的距离"""
    h1, s1, v1 = rgb_to_hsv(color1)
    h2, s2, v2 = rgb_to_hsv(color2)
    
    # 色调在环形空间中，需要特殊处理
    h_dist = min(abs(h1 - h2), 1 - abs(h1 - h2))
    
    # 综合距离，给予色调更高的权重
    return h_dist * 5 + abs(s1 - s2) + abs(v1 - v2)

def find_dominant_macaron_colors(image, num_colors=5):
    """
    从图像中提取主要颜色并调整为马卡龙风格：
    1. 过滤掉黑白灰颜色
    2. 从剩余颜色中找到出现频率最高的几种
    3. 调整这些颜色使其接近马卡龙风格
    4. 确保提取的颜色之间有足够的差异
    """
    # 缩小图片以提高效率
    img = image.copy()
    img.thumbnail((150, 150))
    img = img.convert('RGB')
    pixels = list(img.getdata())
    
    # 过滤掉黑白灰颜色
    filtered_pixels = [p for p in pixels if is_not_black_white_gray_near(p)]
    if not filtered_pixels:
        return []
    
    # 统计颜色出现频率
    color_counter = Counter(filtered_pixels)
    candidate_colors = color_counter.most_common(num_colors * 5)  # 提取更多候选颜色
    
    macaron_colors = []
    min_color_distance = 0.15  # 颜色差异阈值
    
    for color, _ in candidate_colors:
        # 调整为马卡龙风格
        adjusted_color = adjust_color_macaron(color)
        
        # 检查与已选颜色的差异
        if not any(color_distance(adjusted_color, existing) < min_color_distance for existing in macaron_colors):
            macaron_colors.append(adjusted_color)
            if len(macaron_colors) >= num_colors:
                break
    
    return macaron_colors

def adjust_background_color(color, darken_factor=0.85):
    """
    调整背景色，使其适合作为背景：
    - 降低亮度以减少对比度
    - 略微降低饱和度
    """
    h, s, v = rgb_to_hsv(color)
    # 降低亮度
    v = v * darken_factor
    # 略微降低饱和度
    s = s * 0.9
    return hsv_to_rgb(h, s, v)

def darken_color(color, factor=0.7):
    """
    将颜色加深。
    """
    r, g, b = color
    return (int(r * factor), int(g * factor), int(b * factor))

def add_film_grain(image, intensity=0.05):
    """添加胶片颗粒效果"""
    img_array = np.array(image)
    
    # 创建随机噪点
    noise = np.random.normal(0, intensity * 255, img_array.shape)
    
    # 应用噪点
    img_array = img_array + noise
    img_array = np.clip(img_array, 0, 255).astype(np.uint8)
    
    return Image.fromarray(img_array)

def crop_to_square(img):
    """将图片裁剪为正方形"""
    width, height = img.size
    size = min(width, height)
    
    left = (width - size) // 2
    top = (height - size) // 2
    right = left + size
    bottom = top + size
    
    return img.crop((left, top, right, bottom))
    
def add_rounded_corners(img, radius=30):
    """
    给图片添加圆角，通过超采样技术消除锯齿
    
    Args:
        img: PIL.Image对象
        radius: 圆角半径
        
    Returns:
        带圆角的图片(RGBA模式)
    """
    # 超采样倍数
    factor = 2
    
    # 获取原始尺寸
    width, height = img.size
    
    # 创建更大尺寸的空白图像（用于超采样）
    enlarged_img = img.resize((width * factor, height * factor), Image.Resampling.LANCZOS)
    enlarged_img = enlarged_img.convert("RGBA")
    
    # 创建透明蒙版，尺寸为放大后的尺寸
    mask = Image.new('L', (width * factor, height * factor), 0)
    draw = ImageDraw.Draw(mask)
    
    draw.rounded_rectangle([(0, 0), (width * factor, height * factor)], 
                            radius=radius * factor, fill=255)
    
    # 创建超采样尺寸的透明背景
    background = Image.new("RGBA", (width * factor, height * factor), (255, 255, 255, 0))
    
    # 使用蒙版合成图像（在高分辨率下）
    high_res_result = Image.composite(enlarged_img, background, mask)
    
    # 将结果缩小回原来的尺寸，应用抗锯齿
    result = high_res_result.resize((width, height), Image.Resampling.LANCZOS)
    
    return result

def add_card_shadow(img, offset=(10, 10), radius=10, opacity=0.5):
    """给卡片添加更真实的阴影效果"""
    # 获取原图尺寸
    width, height = img.size
    
    # 创建一个更大的画布以容纳阴影和旋转后的图像
    # 提供足够的边距，确保旋转后阴影不会被截断
    padding = max(width, height) // 2
    shadow = Image.new("RGBA", (width + padding * 2, height + padding * 2), (0, 0, 0, 0))
    
    # 在原图轮廓绘制黑色阴影，放置在中心偏移的位置
    orig_mask = Image.new("L", (width, height), 255)
    rounded_mask = add_rounded_corners(orig_mask, radius).convert("L")
    
    # 阴影位置计算，从中心位置开始偏移
    shadow_x = padding + offset[0]
    shadow_y = padding + offset[1]
    shadow.paste((0, 0, 0, int(255 * opacity)), 
                (shadow_x, shadow_y, width + shadow_x, height + shadow_y), 
                rounded_mask)
    
    # 模糊阴影以获得更自然的效果
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
    
    # 创建结果图像
    result = Image.new("RGBA", shadow.size, (0, 0, 0, 0))
    
    # 先放置阴影
    result.paste(shadow, (0, 0), shadow)
    
    # 放置原图到中心位置
    result.paste(img, (padding, padding), img if img.mode == "RGBA" else None)
    
    return result

def add_shadow_and_rotate(canvas, img, angle, offset=(10, 10), radius=10, opacity=0.5, center_pos=None):
    """
    先创建阴影并旋转放置，然后旋转图像并放置
    
    Args:
        canvas: 目标画布
        img: 需要处理的图像
        angle: 旋转角度
        offset: 阴影偏移
        radius: 阴影模糊半径
        opacity: 阴影透明度
        center_pos: 放置中心位置 (x, y)
        
    Returns:
        更新后的画布
    """
    # 获取原图尺寸
    width, height = img.size
    
    # 如果没有指定中心位置，默认使用画布中心
    if center_pos is None:
        center_pos = (canvas.width // 2, canvas.height // 2)
    
    # 1. 创建阴影
    # 创建一个更大的阴影画布，给阴影留足空间，避免截断
    padding = max(radius * 4, 100)  # 为阴影提供足够的空间
    shadow_size = (width + padding * 2, height + padding * 2)
    shadow = Image.new("RGBA", shadow_size, (0, 0, 0, 0))
    
    # 准备阴影蒙版
    mask_size = (width, height)
    shadow_mask = Image.new("L", mask_size, 255)  # 白色蒙版
    
    # 如果原图是RGBA模式，使用其透明通道作为蒙版
    if img.mode == "RGBA":
        shadow_mask = img.split()[3]  # 获取Alpha通道作为蒙版
    
    # 在阴影中心位置创建阴影形状
    shadow_center = (padding, padding)
    shadow.paste((0, 0, 0, int(255 * opacity)), 
                (shadow_center[0], shadow_center[1], 
                 shadow_center[0] + width, shadow_center[1] + height), 
                shadow_mask)
    
    # 模糊阴影，使用较大的半径确保柔和效果
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
    
    # 2. 旋转阴影和图像
    # 旋转阴影
    rotated_shadow = rotate_image(shadow, angle)
    shadow_width, shadow_height = rotated_shadow.size
    
    # 计算旋转后的阴影位置（考虑偏移）
    shadow_x = center_pos[0] - shadow_width // 2 + offset[0]
    shadow_y = center_pos[1] - shadow_height // 2 + offset[1]
    
    # 将阴影粘贴到画布上
    canvas.paste(rotated_shadow, (shadow_x, shadow_y), rotated_shadow)
    
    # 旋转原图
    rotated_img = rotate_image(img, angle)
    img_width, img_height = rotated_img.size
    
    # 计算旋转后的图片位置
    img_x = center_pos[0] - img_width // 2
    img_y = center_pos[1] - img_height // 2
    
    # 将图片粘贴到画布上
    canvas.paste(rotated_img, (img_x, img_y), rotated_img)
    
    return canvas

def rotate_image(img, angle, bg_color=(0, 0, 0, 0)):
    """旋转图片并确保不会截断图片内容"""
    # expand=True 确保旋转后的图片不会被截断
    return img.rotate(angle, Image.BICUBIC, expand=True, fillcolor=bg_color)

def add_badge_to_image(image, number, font_path=None, font_size=1.0,
                      position='top-left', bg_color='#FF0000', text_color=None, padding=10):
    """
    在图像上添加圆角矩形角标
    
    Args:
        image: PIL.Image对象
        number: 角标数字（媒体总数），可以为0
        font_path: 角标字体路径
        font_size: 角标字体大小比例
        position: 角标位置 ('top-left', 'top-right', 'bottom-left', 'bottom-right')
        bg_color: 角标背景颜色
        text_color: 角标文字颜色（如果为None，则使用白色）
        padding: 角标内边距
        
    Returns:
        带角标的图像
    """
    try:
        # 将数字转换为字符串，即使为0也显示
        number_str = str(number)
        if number > 9999:
            number_str = "9999+"
        
        # 如果数字为0，仍然显示角标
        if number < 0:
            # 如果数字为负数（表示获取失败），不显示角标
            return image
        
        # 创建绘制对象
        draw = ImageDraw.Draw(image)
        
        # 解析背景颜色
        if bg_color.startswith('#'):
            bg_color = bg_color.lstrip('#')
            if len(bg_color) == 6:
                r = int(bg_color[0:2], 16)
                g = int(bg_color[2:4], 16)
                b = int(bg_color[4:6], 16)
            else:
                r, g, b = 255, 0, 0  # 默认红色
        else:
            # 尝试解析RGB格式
            try:
                if ',' in bg_color:
                    r, g, b = [int(c.strip()) for c in bg_color.strip('()').split(',')[:3]]
                else:
                    r, g, b = 255, 0, 0  # 默认红色
            except:
                r, g, b = 255, 0, 0  # 默认红色
        
        badge_bg_color = (r, g, b)
        
        # 计算角标大小
        image_width, image_height = image.size
        
        # 基础角标大小（基于图像尺寸的百分比）
        base_size = min(image_width, image_height) * 0.06 * font_size
        
        # 加载字体
        font = None
        if font_path and os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, int(base_size))
            except Exception as e:
                logger.warning(f"加载角标字体失败 {font_path}: {e}")
                font = None
        
        # 如果字体加载失败，尝试使用默认字体
        if font is None:
            try:
                # 尝试加载系统字体
                font = ImageFont.truetype("arial.ttf", int(base_size))
            except:
                try:
                    # 尝试加载其他系统字体
                    font = ImageFont.truetype("DejaVuSans.ttf", int(base_size))
                except:
                    # 使用PIL默认字体
                    font = ImageFont.load_default()
        
        # 计算文本尺寸
        bbox = draw.textbbox((0, 0), number_str, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        # 计算圆角矩形尺寸（文本宽高加上内边距）
        rect_width = text_width + padding * 2
        rect_height = text_height + padding * 2
        
        # 计算圆角半径（高度的30%）
        corner_radius = int(rect_height * 0.3)
        
        # 计算角标位置
        margin = 40  # 边距
        
        if position == 'top-left':
            rect_x = margin
            rect_y = margin
        elif position == 'top-right':
            rect_x = image_width - margin - rect_width
            rect_y = margin
        elif position == 'bottom-left':
            rect_x = margin
            rect_y = image_height - margin - rect_height
        elif position == 'bottom-right':
            rect_x = image_width - margin - rect_width
            rect_y = image_height - margin - rect_height
        else:
            # 默认左上角
            rect_x = margin
            rect_y = margin
        
        # 计算文本位置（居中对齐）
        text_x = rect_x + (rect_width - text_width) // 2
        text_y = rect_y + (rect_height - text_height) // 2
        
        # 解析文字颜色（如果未提供，则使用白色）
        if text_color:
            if text_color.startswith('#'):
                text_color = text_color.lstrip('#')
                if len(text_color) == 6:
                    text_r = int(text_color[0:2], 16)
                    text_g = int(text_color[2:4], 16)
                    text_b = int(text_color[4:6], 16)
                else:
                    text_r, text_g, text_b = 255, 255, 255  # 默认白色
            else:
                # 尝试解析RGB格式
                try:
                    if ',' in text_color:
                        text_r, text_g, text_b = [int(c.strip()) for c in text_color.strip('()').split(',')[:3]]
                    else:
                        text_r, text_g, text_b = 255, 255, 255  # 默认白色
                except:
                    text_r, text_g, text_b = 255, 255, 255  # 默认白色
        else:
            text_r, text_g, text_b = 255, 255, 255  # 默认白色
        
        text_fg_color = (text_r, text_g, text_b)
        
        # 创建圆角矩形角标
        # 绘制圆角矩形背景，带透明度
        badge_color_with_alpha = badge_bg_color + (220,)  # 85% 不透明度
        
        # 绘制圆角矩形
        draw.rounded_rectangle(
            [(rect_x, rect_y), (rect_x + rect_width, rect_y + rect_height)],
            radius=corner_radius,
            fill=badge_color_with_alpha
        )
        
        # 添加一点阴影效果
        shadow_offset = 2
        shadow_rect = [(rect_x + shadow_offset, rect_y + shadow_offset), 
                      (rect_x + rect_width + shadow_offset, rect_y + rect_height + shadow_offset)]
        shadow_color_with_alpha = (0, 0, 0, 80)  # 黑色半透明阴影
        draw.rounded_rectangle(shadow_rect, radius=corner_radius, fill=shadow_color_with_alpha)
        
        # 重新绘制前景矩形（覆盖阴影的上半部分）
        draw.rounded_rectangle(
            [(rect_x, rect_y), (rect_x + rect_width, rect_y + rect_height)],
            radius=corner_radius,
            fill=badge_color_with_alpha
        )
        
        # 绘制文本（使用指定的文字颜色，带一点阴影效果）
        # 文本阴影
        text_shadow_offset = 1
        draw.text((text_x + text_shadow_offset, text_y + text_shadow_offset), 
                 number_str, fill=(0, 0, 0, 100), font=font, align='center')
        
        # 文本前景
        draw.text((text_x, text_y), number_str, fill=text_fg_color, font=font, align='center')
        
        return image
        
    except Exception as e:
        logger.error(f"添加角标失败: {str(e)}")
        return image
    
def create_style_single_1(image_path, title, font_path, font_size=(1,1), blur_size=50, color_ratio=0.8,
                         badge_number=None, badge_font_path=None, badge_font_size=1.0,
                         badge_position='top-left', badge_color='#FF0000', badge_text_color=None, badge_padding=10):
    """
    创建单图样式1的封面，支持圆角矩形角标功能
    
    Args:
        image_path: 图片路径
        title: 标题元组 (中文标题, 英文标题)
        font_path: 字体路径元组 (中文字体路径, 英文字体路径)
        font_size: 字体大小比例元组 (中文比例, 英文比例)
        blur_size: 背景模糊尺寸
        color_ratio: 颜色混合比例
        badge_number: 角标数字（媒体总数）
        badge_font_path: 角标字体路径
        badge_font_size: 角标字体大小比例
        badge_position: 角标位置 ('top-left', 'top-right', 'bottom-left', 'bottom-right')
        badge_color: 角标背景颜色
        badge_text_color: 角标文字颜色（如果为None，则使用白色）
        badge_padding: 角标内边距
        
    Returns:
        Base64编码的图片数据
    """
    try:
        zh_font_path, en_font_path = font_path
        title_zh, title_en = title
        zh_font_size_ratio, en_font_size_ratio = font_size

        if int(blur_size) < 0:
            blur_size = 50

        if float(color_ratio) < 0 or float(color_ratio) > 1:
            color_ratio = 0.8

        if not float(zh_font_size_ratio) > 0:
            zh_font_size_ratio = 1
        if not float(en_font_size_ratio) > 0:
            en_font_size_ratio = 1

        
        num_colors = 6
        # 加载原始图片
        original_img = Image.open(image_path).convert("RGB")
        
        # 从图片提取马卡龙风格的颜色
        candidate_colors = find_dominant_macaron_colors(original_img, num_colors=num_colors)
        
        random.shuffle(candidate_colors)
        extracted_colors = candidate_colors[:num_colors]
            
        # 柔和的马卡龙备选颜色
        soft_macaron_colors = [
            (237, 159, 77),    # 杏色
            (186, 225, 255),   # 淡蓝色
            (255, 223, 186),   # 浅橘色
            (202, 231, 200),   # 淡绿色
        ]
        
        # 确保有足够的颜色
        while len(extracted_colors) < num_colors:
            # 从备选颜色中选择一个与已有颜色差异最大的
            if not extracted_colors:
                extracted_colors.append(random.choice(soft_macaron_colors))
            else:
                max_diff = 0
                best_color = None
                for color in soft_macaron_colors:
                    min_dist = min(color_distance(color, existing) for existing in extracted_colors)
                    if min_dist > max_diff:
                        max_diff = min_dist
                        best_color = color
                extracted_colors.append(best_color or random.choice(soft_macaron_colors))
        
        # 处理颜色
        bg_color = darken_color(extracted_colors[0], 0.85)  # 背景色
        card_colors = [extracted_colors[1], extracted_colors[2]]  # 卡片颜色
        
        # 2. 背景处理
        bg_img = original_img.copy()
        bg_img = ImageOps.fit(bg_img, canvas_size, method=Image.LANCZOS)
        bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=int(blur_size)))  # 强烈模糊化
        
        # 将背景图片与背景色混合
        bg_img_array = np.array(bg_img, dtype=float)
        bg_color_array = np.array([[bg_color]], dtype=float)
        
        # 混合背景图和颜色 (15% 背景图 + 85% 颜色)
        blended_bg = bg_img_array * (1 - float(color_ratio)) + bg_color_array * float(color_ratio)
        blended_bg = np.clip(blended_bg, 0, 255).astype(np.uint8)
        blended_bg_img = Image.fromarray(blended_bg)
        
        # 添加胶片颗粒效果增强纹理感
        blended_bg_img = add_film_grain(blended_bg_img, intensity=0.03)
        
        # 创建最终画布
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        canvas.paste(blended_bg_img)
        
        # 3. 处理卡片效果
        # 裁剪为正方形
        square_img = crop_to_square(original_img)
        
        # 计算卡片尺寸 (画布高度的60%)
        card_size = int(canvas_size[1] * 0.7)
        square_img = square_img.resize((card_size, card_size), Image.LANCZOS)
        
        # 准备三张卡片图像
        cards = []
        
        # 主卡片 - 原始图
        main_card = add_rounded_corners(square_img, radius=card_size//8)
        main_card = main_card.convert("RGBA")
        
        # 辅助卡片1 (中间层) - 与第二种颜色混合，加深颜色
        aux_card1 = square_img.copy().filter(ImageFilter.GaussianBlur(radius=8))
        aux_card1_array = np.array(aux_card1, dtype=float)
        card_color1_array = np.array([[card_colors[0]]], dtype=float)
        # 降低原图比例，增加颜色混合比例
        blended_card1 = aux_card1_array * 0.5 + card_color1_array * 0.5
        blended_card1 = np.clip(blended_card1, 0, 255).astype(np.uint8)
        aux_card1 = Image.fromarray(blended_card1)
        aux_card1 = add_rounded_corners(aux_card1, radius=card_size//8)
        aux_card1 = aux_card1.convert("RGBA")
        
        # 辅助卡片2 (底层) - 与第三种颜色混合，加深颜色
        aux_card2 = square_img.copy().filter(ImageFilter.GaussianBlur(radius=16))
        aux_card2_array = np.array(aux_card2, dtype=float)
        card_color2_array = np.array([[card_colors[1]]], dtype=float)
        # 降低原图比例，增加颜色混合比例
        blended_card2 = aux_card2_array * 0.4 + card_color2_array * 0.6
        blended_card2 = np.clip(blended_card2, 0, 255).astype(np.uint8)
        aux_card2 = Image.fromarray(blended_card2)
        aux_card2 = add_rounded_corners(aux_card2, radius=card_size//8)
        aux_card2 = aux_card2.convert("RGBA")
        
        # 4. 分别添加阴影和旋转
        # 计算卡片放置中心位置 (画布右侧)
        center_x = int(canvas_size[0] - canvas_size[1] * 0.5)  # 稍微左移，给旋转后的卡片留出空间
        center_y = int(canvas_size[1] * 0.5)
        center_pos = (center_x, center_y)
        
        # 按照需求指定旋转角度
        rotation_angles = [36, 18, 0]  # 底层、中间层、顶层的旋转角度
        
        # 阴影配置
        shadow_configs = [
            {'offset': (10, 16), 'radius': 12, 'opacity': 0.4},  # 底层卡片阴影配置
            {'offset': (15, 22), 'radius': 15, 'opacity': 0.5},  # 中间层卡片阴影配置
            {'offset': (20, 26), 'radius': 18, 'opacity': 0.6},  # 顶层卡片阴影配置
        ]
        
        # 创建一个临时画布，用于叠加卡片和阴影效果
        cards_canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        
        # 从底层到顶层依次添加阴影和卡片
        cards = [aux_card2, aux_card1, main_card]
        
        for i, (card, angle, shadow_config) in enumerate(zip(cards, rotation_angles, shadow_configs)):
            # 使用优化后的函数添加阴影和旋转图片
            cards_canvas = add_shadow_and_rotate(
                cards_canvas, 
                card, 
                angle, 
                offset=shadow_config['offset'], 
                radius=shadow_config['radius'], 
                opacity=shadow_config['opacity'],
                center_pos=center_pos
            )
        
        # 将裁剪后的卡片画布与背景合并
        canvas = Image.alpha_composite(canvas.convert("RGBA"), cards_canvas)
        
        # 5. 文字处理
        text_layer = Image.new('RGBA', canvas_size, (255, 255, 255, 0))
        shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))

        shadow_draw = ImageDraw.Draw(shadow_layer)
        draw = ImageDraw.Draw(text_layer)
        
        # 计算左侧区域的中心 X 位置 (画布宽度的四分之一处)
        left_area_center_x = int(canvas_size[0] * 0.25)
        left_area_center_y = canvas_size[1] // 2
        
        zh_font_size = int(canvas_size[1] * 0.17 * float(zh_font_size_ratio))
        en_font_size = int(canvas_size[1] * 0.07 * float(en_font_size_ratio))
        
        zh_font = ImageFont.truetype(zh_font_path, zh_font_size)
        en_font = ImageFont.truetype(en_font_path, en_font_size)
        
        # 文字颜色和阴影颜色
        text_color = (255, 255, 255, 229)  # 85% 不透明度
        shadow_color = darken_color(bg_color, 0.8) + (75,)  # 阴影颜色加透明度
        shadow_offset = 12
        shadow_alpha = 75
        
        # 计算中文标题的位置
        zh_bbox = draw.textbbox((0, 0), title_zh, font=zh_font)
        zh_text_w = zh_bbox[2] - zh_bbox[0]
        zh_text_h = zh_bbox[3] - zh_bbox[1]
        zh_x = left_area_center_x - zh_text_w // 2
        zh_y = left_area_center_y - zh_text_h - en_font_size // 2 - 5
        
        # 中文标题阴影效果
        for offset in range(3, shadow_offset + 1, 2):
            current_shadow_color = shadow_color[:3] + (shadow_alpha,)
            shadow_draw.text((zh_x + offset, zh_y + offset), title_zh, font=zh_font, fill=current_shadow_color)
        
        # 中文标题
        draw.text((zh_x, zh_y), title_zh, font=zh_font, fill=text_color)
        
        if title_en:
            # 计算英文标题的位置
            en_bbox = draw.textbbox((0, 0), title_en, font=en_font)
            en_text_w = en_bbox[2] - en_bbox[0]
            en_text_h = en_bbox[3] - en_bbox[1]
            en_x = left_area_center_x - en_text_w // 2
            en_y = zh_y + zh_text_h + en_font_size  # 调整英文标题位置，与中文标题有一定间距
            
            # 英文标题阴影效果
            for offset in range(2, shadow_offset // 2 + 1):
                current_shadow_color = shadow_color[:3] + (shadow_alpha,)
                shadow_draw.text((en_x + offset, en_y + offset), title_en, font=en_font, fill=current_shadow_color)
            
            # 英文标题
            draw.text((en_x, en_y), title_en, font=en_font, fill=text_color)
        
        blurred_shadow = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_offset))
        combined = Image.alpha_composite(canvas, blurred_shadow)
        # 合并所有图层
        combined = Image.alpha_composite(combined, text_layer)
        
        # 6. 添加圆角矩形角标（如果需要）
        if badge_number is not None and badge_number > 0:
            combined = add_badge_to_image(
                combined,
                number=badge_number,
                font_path=badge_font_path,
                font_size=badge_font_size,
                position=badge_position,
                bg_color=badge_color,
                text_color=badge_text_color,  # 新增：传递文字颜色
                padding=badge_padding
            )
        
        def image_to_base64(image, format="auto", quality=85):
            buffer = BytesIO()
            if format.lower() == "auto":
                if image.mode == "RGBA" or (image.info.get('transparency') is not None):
                    format = "PNG"
                else:
                    try:
                        image.save(buffer, format="WEBP", quality=quality, optimize=True)
                        base64_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
                        return base64_str
                    except Exception:
                        format = "JPEG" # Fallback to JPEG if WebP fails
            if format.lower() == "png":
                image.save(buffer, format="PNG", optimize=True)
                base64_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
                return base64_str
            elif format.lower() == "jpeg":
                image = image.convert("RGB") # Ensure RGB for JPEG
                image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
                base64_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
                return base64_str
            else:
                raise ValueError(f"Unsupported format: {format}")
            
        return image_to_base64(combined)
        
    except Exception as e:
        logger.error(f"创建单图封面时出错: {e}")
        return False